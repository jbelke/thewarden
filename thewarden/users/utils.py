import configparser
import csv
import glob
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from flask import flash, url_for
from flask_login import current_user
from flask_mail import Message

from thewarden import db, mail
from thewarden import mhp as mrh
from thewarden.models import Trades
from thewarden.node.utils import tor_request
from thewarden.pricing_engine.pricing import (FX_PROVIDER_PRIORITY,
                                              HISTORICAL_PROVIDER_PRIORITY,
                                              PROVIDER_LIST, PriceData,
                                              api_keys_class,
                                              multiple_price_grab, price_data,
                                              price_data_fx, price_data_rt)
from thewarden.users.decorators import MWT, memoized, timing

# ---------------------------------------------------------
# Helper Functions start here
# ---------------------------------------------------------

# --------------------------------------------
# Read Global Variables from config(s)
# Include global variables and error handling
# --------------------------------------------
config = configparser.ConfigParser()
config.read('config.ini')
try:
    RENEW_NAV = config['MAIN']['RENEW_NAV']
except KeyError:
    RENEW_NAV = 10
    logging.error("Could not find RENEW_NAV at config.ini. Defaulting to 60.")
try:
    PORTFOLIO_MIN_SIZE_NAV = config['MAIN']['PORTFOLIO_MIN_SIZE_NAV']
except KeyError:
    PORTFOLIO_MIN_SIZE_NAV = 5
    logging.error("Could not find PORTFOLIO_MIN_SIZE_NAV at config.ini." +
                  " Defaulting to 5.")


@MWT(timeout=5)
@timing
def cost_calculation(ticker):
    # This function calculates the cost basis assuming 3 different methods
    # FIFO, LIFO and avg. cost
    # found = [item for item in fx_list() if ticker in item]
    # if found != []:
    #     return (0)

    # Gets all transactions in local currency terms
    df = transactions_fx()
    df = df[(df.trade_asset_ticker == ticker)]

    # Find current open position on asset
    summary_table = df.groupby(['trade_asset_ticker', 'trade_operation'])[
        ["cash_value", "cash_value_fx", "trade_fees", "trade_quantity"]].sum()

    open_position = summary_table.sum()['trade_quantity']

    # Drop Deposits and Withdraws - keep only Buy and Sells
    if open_position > 0:
        df = df[df.trade_operation.str.match('B')]
    elif open_position < 0:
        df = df[df.trade_operation.str.match('S')]

    # Let's return a dictionary for this user with FIFO, LIFO and Avg. Cost
    cost_matrix = {}

    # ---------------------------------------------------
    # FIFO
    # ---------------------------------------------------
    fifo_df = df.sort_index(ascending=False)
    fifo_df['acum_Q'] = fifo_df['trade_quantity'].cumsum()
    fifo_df['acum_Q'] = np.where(fifo_df['acum_Q'] < open_position,
                                 fifo_df['acum_Q'], open_position)
    # Keep only the number of rows needed for open position
    fifo_df = fifo_df.drop_duplicates(subset="acum_Q", keep='first')
    fifo_df['Q'] = fifo_df['acum_Q'].diff()
    fifo_df['Q'] = fifo_df['Q'].fillna(fifo_df['trade_quantity'])
    # if fifo_df['acum_Q'].count() == 1:
    #     fifo_df['Q'] = fifo_df['acum_Q']
    # Adjust Cash Value only to account for needed position
    fifo_df['adjusted_cv'] = fifo_df['cash_value_fx'] * fifo_df['Q'] /\
        fifo_df['trade_quantity']
    cost_matrix['FIFO'] = {}
    cost_matrix['FIFO']['FIFO_cash'] = fifo_df['adjusted_cv'].sum()
    cost_matrix['FIFO']['FIFO_quantity'] = open_position
    cost_matrix['FIFO']['FIFO_count'] = int(fifo_df['trade_operation'].count())
    cost_matrix['FIFO']['FIFO_average_cost'] = fifo_df['adjusted_cv'].sum()\
        / open_position

    # ---------------------------------------------------
    #  LIFO
    # ---------------------------------------------------
    lifo_df = df.sort_index(ascending=True)
    lifo_df['acum_Q'] = lifo_df['trade_quantity'].cumsum()
    lifo_df['acum_Q'] = np.where(lifo_df['acum_Q'] < open_position,
                                 lifo_df['acum_Q'], open_position)
    # Keep only the number of rows needed for open position
    lifo_df = lifo_df.drop_duplicates(subset="acum_Q", keep='first')
    lifo_df['Q'] = lifo_df['acum_Q'].diff()
    lifo_df['Q'] = lifo_df['Q'].fillna(lifo_df['trade_quantity'])
    # if lifo_df['acum_Q'].count() == 1:
    #     lifo_df['Q'] = lifo_df['acum_Q']
    # Adjust Cash Value only to account for needed position
    lifo_df['adjusted_cv'] = lifo_df['cash_value_fx'] * lifo_df['Q'] /\
        lifo_df['trade_quantity']

    cost_matrix['LIFO'] = {}
    cost_matrix['LIFO']['LIFO_cash'] = lifo_df['adjusted_cv'].sum()
    cost_matrix['LIFO']['LIFO_quantity'] = open_position
    cost_matrix['LIFO']['LIFO_count'] = int(lifo_df['trade_operation'].count())
    cost_matrix['LIFO']['LIFO_average_cost'] = lifo_df['adjusted_cv'].sum() / open_position
    return (cost_matrix)


def to_epoch(in_date):
    return str(int((in_date - datetime(1970, 1, 1)).total_seconds()))


def find_fx(row, fx=None):
    # row.name is the date being passed
    # row['trade_currency'] is the base fx (the one where the trade was included)
    # Create an instance of PriceData:
    fx_df = PriceData(row['trade_currency'])
    fx = fx_df.price_ondate(row.name)
    return price_ondate(current_user.fx(), row.name, row['trade_currency'])


@MWT(timeout=3)
@timing
def transactions_fx():
    # Gets the transaction table and fills with fx information
    # Note that it uses the currency exchange for the date of transaction
    # Get all transactions from db and format
    df = pd.read_sql_table('trades', db.engine)
    df = df[(df.user_id == current_user.username)]
    # df = df[(df.trade_operation == "B") | (df.trade_operation == "S")]
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    df = df.set_index('trade_date')
    # Ignore times in df to merge - keep only dates
    df.index = df.index.floor('d')
    df.index.rename('date', inplace=True)
    # The current fx needs no conversion, set to 1
    df[current_user.fx()] = 1
    # Need to get currencies into the df in order to normalize
    # let's load a list of currencies needed and merge
    list_of_fx = df.trade_currency.unique().tolist()
    # loop through currency list
    for currency in list_of_fx:
        if currency == current_user.fx():
            continue
        # Make a price request
        df[currency] = df.apply(find_fx, axis=1)
    # Now create a cash value in the preferred currency terms
    df['fx'] = df.apply(lambda x: x[x['trade_currency']], axis=1)
    df['cash_value_fx'] = df['cash_value'].astype(float) / df['fx'].astype(float)
    df['trade_fees_fx'] = df['trade_fees'].astype(float) / df['fx'].astype(float)
    df['trade_price_fx'] = df['trade_price'].astype(float) / df['fx'].astype(float)
    return (df)


@memoized
def is_currency(id):
    # Return true if id is in list of currencies
    found = ([item for item in fx_list() if item[0] == id])
    if found != []:
        return True
    return False


# ---------------- PANDAS HELPER FUNCTION --------------------------
# This is a function to concatenate a function returning multiple columns into
# a dataframe.
def apply_and_concat(dataframe, field, func, column_names):
    return pd.concat((
        dataframe,
        dataframe[field].apply(
            lambda cell: pd.Series(func(cell), index=column_names))), axis=1)


# ---------------- PANDAS HELPER FUNCTION --------------------------
# Pandas helper function to unpack a dictionary stored within a
# single pandas column cells.
def df_unpack(df, column, fillna=None):
    ret = None
    if fillna is None:
        ret = pd.concat([df, pd.DataFrame(
            (d for idx, d in df[column].iteritems()))], axis=1)
        del ret[column]
    else:
        ret = pd.concat([df, pd.DataFrame(
            (d for idx, d in df[column].iteritems())).fillna(fillna)], axis=1)
        del ret[column]
    print(ret)
    return ret



@MWT(timeout=1)
@timing
def positions():
    # Method to create a user's position table
    # Returns a df with the following information
    # Ticker, name, quantity, small_pos
    # THIS SHOULD CONTAIN THE STATIC FIELDS ONLY - no web requests
    # It should be a light method to load quickly on the main page.
    # Anything with web requests should be done on a separate function

    # Get all transactions & group by ticker name and operation
    df = transactions_fx()
    summary_table = df.groupby(['trade_asset_ticker', 'trade_operation'])[
                                ["trade_quantity",
                                 "cash_value_fx",
                                 "trade_fees_fx"]].sum()
    # Now let's create our main dataframe with information for each ticker
    list_of_tickers = df.trade_asset_ticker.unique().tolist()
    main_df = pd.DataFrame({'trade_asset_ticker': list_of_tickers})
    # Fill with positions, cash_values and fees
    df_tmp = df.groupby(['trade_asset_ticker'])[
                        ["trade_quantity",
                         "cash_value_fx",
                         "trade_fees_fx"]].sum()
    main_df = pd.merge(main_df, df_tmp, on='trade_asset_ticker')
    # Fill in with same information but only for buys, sells, deposits and withdraws
    # main_df = pd.merge(main_df, summary_table, on='trade_asset_ticker')
    summary_table = summary_table.unstack(level='trade_operation').fillna(0)
    main_df = pd.merge(main_df, summary_table, on='trade_asset_ticker')
    # Include FIFO and LIFO calculations for each ticker
    main_df['cost_frame'] = main_df['trade_asset_ticker'].apply(cost_calculation)
    # Unpack this into multiple columns now
    main_df = df_unpack(main_df, 'cost_frame', 0)
    main_df = df_unpack(main_df, 'FIFO', 0)
    main_df = df_unpack(main_df, 'LIFO', 0)
    main_df['is_currency'] = main_df['trade_asset_ticker'].apply(is_currency)
    return main_df



# This function is used to grab a single price that was missing from
# the multiprice request. Since this is a bit more time intensive, it's
# separated so it can be memoized for a period of time (this price will
# not refresh as frequently)
# default: timeout=30
@MWT(timeout=30)
def single_price(ticker):
    return (price_data_rt(ticker), datetime.now())


def positions_dynamic():
    # This method is the realtime updater for the front page. It gets the
    # position information from positions above and returns a dataframe
    # with all the realtime pricing and positions data - this method
    # should be called from an AJAX request at the front page in order
    # to reduce loading time.
    df = positions()
    # Drop all currencies from table
    df = df[df['is_currency'] == False]
    list_of_tickers = df.trade_asset_ticker.unique().tolist()
    tickers_string = ",".join(list_of_tickers)
    # Let's try to get as many prices as possible into the df with a
    # single request - first get all the prices in current currency and USD
    print("REQUESTING MULTI PRICE")
    multi_price = multiple_price_grab(tickers_string, 'USD,' + current_user.fx())

    # PARSER Function to fing the ticker price inside the matrix. First part
    # looks into the cryptocompare matrix. In the exception, if price is not
    # found, it sends a request to other providers
    def find_data(ticker):
        try:
            price = multi_price["RAW"][ticker][current_user.fx()]["PRICE"]
            price = float(price * current_user.fx_rate_USD())
            high = float(multi_price["RAW"][ticker][
                current_user.fx()]["HIGHDAY"] * current_user.fx_rate_USD())
            low = float(multi_price["RAW"][ticker][
                current_user.fx()]["LOWDAY"] * current_user.fx_rate_USD())
            chg = multi_price["RAW"][ticker][current_user.fx()]["CHANGEPCT24HOUR"]
            mktcap = multi_price["RAW"][ticker][current_user.fx()]["MKTCAP"]
            last_up_source = multi_price["RAW"][ticker][current_user.fx()]["LASTUPDATE"]
            last_update = datetime.now()
        except KeyError as e:
            # Couldn't find price with CryptoCompare. Let's try a different source
            # and populate data in the same format
            try:
                price, last_update = single_price(ticker)
                high = low = chg = mktcap = last_up_source = 0
                if price is None:
                    flash(f"Price for ticker {ticker} not found. Error: {e}", "danger")
                price = float(price * current_user.fx_rate_USD())
            except Exception as e:
                price = high = low = chg = mktcap = last_up_source = 0
                print(f"Ticker: {ticker}. Error {e}")
                flash(f"Ticker: {ticker}. Error {e}")
        return price, last_update, high, low, chg, mktcap, last_up_source
    df = apply_and_concat(df, 'trade_asset_ticker',
                          find_data, ['price', 'last_update', '24h_high', '24h_low',
                                      '24h_change', 'mktcap', 'last_up_source'])

    # Now create additional columns with calculations
    df['position_fx'] = df['price'] * df['trade_quantity']
    df['allocation'] = df['position_fx'] / df['position_fx'].sum()
    df['change_fx'] = df['position_fx'] * df['24h_change'] / 100
    # Pnl and Cost calculations
    df['breakeven'] = df['cash_value_fx'] / df['trade_quantity']
    df['pnl_gross'] = df['position_fx'] - df['cash_value_fx']
    df['pnl_net'] = df['pnl_gross'] - df['trade_fees_fx']
    # FIFO and LIFO PnL calculations
    df['LIFO_unreal'] = (df['price'] - df['LIFO_average_cost']) * \
                        df['trade_quantity']
    df['FIFO_unreal'] = (df['price'] - df['FIFO_average_cost']) * \
                        df['trade_quantity']
    df['LIFO_real'] = df['pnl_net'] - df['LIFO_unreal']
    df['FIFO_real'] = df['pnl_net'] - df['FIFO_unreal']
    df['LIFO_unrealized_be'] = df['price'] - \
                               (df['LIFO_unreal'] / df['trade_quantity'])
    df['FIFO_unrealized_be'] = df['price'] - \
                                (df['FIFO_unreal'] / df['trade_quantity'])


    # Allocations below 0.01% are marked as small
    # this is used to hide small and closed positions at html
    df.loc[df.allocation <= 0.0001, 'small_pos'] = 'True'
    df.loc[df.allocation >= 0.0001, 'small_pos'] = 'False'



    return(df)

@MWT(timeout=5)
@timing
def generate_pos_table(user, fx, hidesmall):
    # This function creates all relevant data for the main page
    # Including current positions, cost and other market info
    # Gets all transactions
    df = transactions_fx()
    # Create string of tickers and grab all prices in one request
    list_of_tickers = df.trade_asset_ticker.unique().tolist()
    ticker_str = ""
    for ticker in list_of_tickers:
        ticker_str = ticker_str + "," + ticker
    price_list = multiple_price_grab(ticker_str, fx)
    from thewarden.pricing_engine.pricing import api_keys_class
    api_keys_json = api_keys_class.loader()
    aa_apikey = api_keys_json['alphavantage']['api_key']
    if price_list == "ConnectionError":
        return ("ConnectionError", "ConnectionError")

    summary_table = df.groupby(['trade_asset_ticker', 'trade_operation'])[
                                ["cash_value", "trade_fees", "trade_quantity",
                                 "cash_value_fx", "trade_fees_fx"]].sum()

    summary_table['count'] = df.groupby([
        'trade_asset_ticker', 'trade_operation'])[
        "cash_value_fx"].count()

    consol_table = df.groupby(['trade_asset_ticker'])[
                                ["cash_value", "trade_fees",
                                 "trade_quantity", "cash_value_fx",
                                 "trade_fees_fx"]].sum()

    consol_table['symbol'] = consol_table.index.values
    try:
        consol_table = consol_table.drop('USD')
        consol_table = consol_table.drop(current_user.fx())
        if consol_table.empty:
            return ("empty", "empty")

    except KeyError:
        logging.info(f"[generate_pos_table] No USD or {current_user.fx()} positions found")

    def find_price_data(ticker):
        try:
            price_data = price_list["RAW"][ticker]['USD']
        except KeyError:
            # Couldn't find price with CryptoCompare. Let's try a different source
            # and populate data in the same format
            base = "https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol="
            baseURL = base + ticker + "&apikey=" + aa_apikey
            data = tor_request(baseURL)
            price_data = {}
            try:
                data = data.json()
                if "Global Quote" in data:
                    price_data['PRICE'] = cleancsv(data['Global Quote']['05. price'])
                    price_data['CHANGEPCT24HOUR'] = cleancsv(data[
                                'Global Quote'][
                                '10. change percent'])
                    price_data['LASTUPDATE'] = "-"
                    price_data['VOLUMEDAY'] = cleancsv(data[
                                'Global Quote']['06. volume'])
                    price_data['LOWDAY'] = cleancsv(data[
                                'Global Quote']['04. low'])
                    price_data['HIGHDAY'] = cleancsv(data[
                                'Global Quote']['03. high'])
                    price_data['MKTCAP'] = 0
                    return (price_data)
            except Exception as e:
                price_data['PRICE'] = 0
                price_data['CHANGEPCT24HOUR'] = 0
                price_data['LASTUPDATE'] = 0
                price_data['VOLUMEDAY'] = 0
                price_data['LOWDAY'] = 0
                price_data['HIGHDAY'] = 0
                price_data['MKTCAP'] = 0
                flash(f"Price for ticker {ticker} not found. Error: {e}", "danger")
        return (price_data)

    def find_price_data_BTC(ticker):
        try:
            price_data = price_list["RAW"][ticker]['BTC']
            return (price_data)
        except KeyError:
            # Couldn't find price with CryptoCompare. Let's try a different source
            # and populate data in the same format
            base = "https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol="
            baseURL = base + ticker + "&apikey=" + aa_apikey
            data = tor_request(baseURL)
            btcURL = "https://www.alphavantage.co/query?" +\
                     "function=CURRENCY_EXCHANGE_RATE&" +\
                     "from_currency=BTC&to_currency=USD&apikey=" +\
                     aa_apikey
            data_BTC = tor_request(btcURL)
            price_data = {}
            try:
                data = data.json()
                data_BTC = data_BTC.json()
                price_BTC = data_BTC[
                    "Realtime Currency Exchange Rate"][
                    "5. Exchange Rate"]
                price_BTC = cleancsv(price_BTC)
                if price_BTC == 0:
                    price_BTC = 1
                if "Global Quote" in data:
                    price_data['PRICE'] = float(
                        cleancsv(data['Global Quote']['05. price']) /
                        price_BTC)
                    if ticker.upper() == "GBTC":
                        # For GBTC, let's calculate the premium to NAV
                        # Each GBTC share contains 0.00099727 bitcoins
                        price_data['GBTC_fair'] = float(price_BTC * 0.00099727) *\
                            current_user.fx_rate_USD()
                        price_data['GBTC_premium'] = float(
                            cleancsv(data['Global Quote']['05. price']) /
                                     price_data['GBTC_fair']) - 1
                    return (price_data)
            except Exception as e:
                price_data['PRICE'] = 0
                flash(f"Price for ticker {ticker} not found. Error: {e}", "danger")
        return (price_data)

    consol_table['price_data_USD'] = consol_table['symbol'].\
        apply(find_price_data)
    consol_table['price_data_BTC'] = consol_table['symbol'].\
        apply(find_price_data_BTC)

    try:
        consol_table['usd_price'] =\
            consol_table.price_data_USD.map(lambda v: v['PRICE'])
    except (KeyError, TypeError) as e:
        flash(f"There was an error generating positions: {e}", "danger")
        consol_table['usd_price'] = 0

    consol_table['fx_price'] = consol_table['usd_price'] * current_user.fx_rate_USD()

    try:
        consol_table['chg_pct_24h'] =\
            consol_table.price_data_USD.map(lambda v: v['CHANGEPCT24HOUR'])
    except KeyError:
        consol_table['chg_pct_24h'] = 0

    try:
        consol_table['btc_price'] =\
            consol_table.price_data_BTC.map(lambda v: v['PRICE'])
    except (TypeError, KeyError):
        consol_table['btc_price'] = 0

    consol_table['usd_position'] = consol_table['usd_price'] *\
        consol_table['trade_quantity']
    consol_table['btc_position'] = consol_table['btc_price'] *\
        consol_table['trade_quantity']
    consol_table['fx_position'] = consol_table['usd_position'] * current_user.fx_rate_USD()

    consol_table['chg_usd_24h'] =\
        consol_table['chg_pct_24h']/100*consol_table['usd_position']
    consol_table['chg_fx_24h'] =\
        consol_table['chg_pct_24h']/100*consol_table['fx_position']

    consol_table['usd_perc'] = consol_table['usd_position']\
        / consol_table['usd_position'].sum()
    consol_table['btc_perc'] = consol_table['btc_position']\
        / consol_table['btc_position'].sum()
    consol_table['fx_perc'] = consol_table['fx_position']\
        / consol_table['fx_position'].sum()

    consol_table.loc[consol_table.fx_perc <= 0.001, 'small_pos'] = 'True'
    consol_table.loc[consol_table.fx_perc >= 0.001, 'small_pos'] = 'False'

    # Should rename this to breakeven:
    consol_table['average_cost'] = consol_table['cash_value']\
        / consol_table['trade_quantity']
    consol_table['average_cost_fx'] = consol_table['cash_value_fx']\
        / consol_table['trade_quantity']

    consol_table['total_pnl_gross_USD'] = consol_table['usd_position'] -\
        consol_table['cash_value']
    consol_table['total_pnl_gross_fx'] = consol_table['fx_position'] -\
        consol_table['cash_value_fx']

    consol_table['total_pnl_net_USD'] = consol_table['usd_position'] -\
        consol_table['cash_value'] - consol_table['trade_fees']
    consol_table['total_pnl_net_fx'] = consol_table['fx_position'] -\
        consol_table['cash_value_fx'] - consol_table['trade_fees_fx']

    summary_table['symbol_operation'] = summary_table.index.values
    # This is wrong:
    summary_table['average_price'] = summary_table['cash_value'] /\
        summary_table['trade_quantity']
    summary_table['average_price_fx'] = summary_table['cash_value_fx'] /\
        summary_table['trade_quantity']

    # create a dictionary in a better format to deliver to html table
    table = {}
    table['TOTAL'] = {}
    table['TOTAL']['fx'] = current_user.fx()
    table['TOTAL']['fx_symbol'] = fxsymbol(current_user.fx())
    table['TOTAL']['cash_flow_value'] = summary_table.sum()['cash_value']
    table['TOTAL']['cash_flow_value_fx'] = summary_table.sum()['cash_value_fx']
    table['TOTAL']['avg_fx_rate'] = table['TOTAL']['cash_flow_value_fx'] /\
        table['TOTAL']['cash_flow_value']
    table['TOTAL']['trade_fees'] = summary_table.sum()['trade_fees']
    table['TOTAL']['trade_fees_fx'] = summary_table.sum()['trade_fees_fx']
    table['TOTAL']['trade_count'] = summary_table.sum()['count']
    table['TOTAL']['usd_position'] = consol_table.sum()['usd_position']
    table['TOTAL']['btc_position'] = consol_table.sum()['btc_position']
    table['TOTAL']['fx_position'] = consol_table.sum()['fx_position']
    table['TOTAL']['chg_usd_24h'] = consol_table.sum()['chg_usd_24h']
    table['TOTAL']['chg_fx_24h'] = consol_table.sum()['chg_fx_24h']
    table['TOTAL']['chg_perc_24h'] = ((table['TOTAL']['chg_usd_24h']
                                       / table['TOTAL']['usd_position']))*100
    table['TOTAL']['chg_perc_24h_fx'] = ((table['TOTAL']['chg_fx_24h']
                                       / table['TOTAL']['fx_position']))*100
    table['TOTAL']['total_pnl_gross_USD'] =\
        consol_table.sum()['total_pnl_gross_USD']
    table['TOTAL']['total_pnl_gross_fx'] =\
        consol_table.sum()['total_pnl_gross_fx']
    table['TOTAL']['total_pnl_net_USD'] =\
        consol_table.sum()['total_pnl_net_USD']
    table['TOTAL']['total_pnl_net_fx'] =\
        consol_table.sum()['total_pnl_net_fx']
    table['TOTAL']['refresh_time'] = datetime.now()
    pie_data = []

    # Drop small positions if hidesmall (small position = <0.01%)
    if hidesmall:
        consol_table = consol_table[consol_table.small_pos == 'False']
        list_of_tickers = consol_table.index.unique().tolist()

    for ticker in list_of_tickers:
        # Check if this is a fiat currency. If so, ignore
        found = [item for item in fx_list() if ticker in item]
        if found != []:
            continue

        table[ticker] = {}
        table[ticker]['breakeven'] = 0
        if consol_table['small_pos'][ticker] == 'False':
            tmp_dict = {}
            tmp_dict['y'] = consol_table['fx_perc'][ticker]*100
            tmp_dict['name'] = ticker
            pie_data.append(tmp_dict)
            table[ticker]['breakeven'] = \
                (consol_table['price_data_USD'][ticker]['PRICE'] *
                current_user.fx_rate_USD()) -\
                (consol_table['total_pnl_net_fx'][ticker] /
                 consol_table['trade_quantity'][ticker])

        table[ticker]['cost_matrix'] = cost_calculation(ticker)
        table[ticker]['cost_matrix']['LIFO']['unrealized_pnl'] = \
            (consol_table['fx_price'][ticker] -
             table[ticker]['cost_matrix']['LIFO']['average_cost']) * \
            consol_table['trade_quantity'][ticker]
        table[ticker]['cost_matrix']['FIFO']['unrealized_pnl'] = \
            (consol_table['fx_price'][ticker] -
             table[ticker]['cost_matrix']['FIFO']['average_cost']) * \
            consol_table['trade_quantity'][ticker]

        table[ticker]['cost_matrix']['LIFO']['realized_pnl'] = \
            consol_table['total_pnl_net_fx'][ticker] -\
            table[ticker]['cost_matrix']['LIFO']['unrealized_pnl']
        table[ticker]['cost_matrix']['FIFO']['realized_pnl'] = \
            consol_table['total_pnl_net_fx'][ticker] -\
            table[ticker]['cost_matrix']['FIFO']['unrealized_pnl']

        table[ticker]['cost_matrix']['LIFO']['unrealized_be'] =\
            (consol_table['price_data_USD'][ticker]['PRICE'] *
            current_user.fx_rate_USD()) - \
            (table[ticker]['cost_matrix']['LIFO']['unrealized_pnl'] /
             consol_table['trade_quantity'][ticker])
        table[ticker]['cost_matrix']['FIFO']['unrealized_be'] =\
            (consol_table['price_data_USD'][ticker]['PRICE'] *
            current_user.fx_rate_USD()) - \
            (table[ticker]['cost_matrix']['FIFO']['unrealized_pnl'] /
             consol_table['trade_quantity'][ticker])

        table[ticker]['position'] = consol_table['trade_quantity'][ticker]
        table[ticker]['usd_position'] = consol_table['usd_position'][ticker]
        table[ticker]['fx_position'] = consol_table['fx_position'][ticker]
        table[ticker]['chg_pct_24h'] = consol_table['chg_pct_24h'][ticker]
        table[ticker]['chg_usd_24h'] = consol_table['chg_usd_24h'][ticker]
        table[ticker]['chg_fx_24h'] = consol_table['chg_fx_24h'][ticker]
        table[ticker]['usd_perc'] = consol_table['usd_perc'][ticker]
        table[ticker]['btc_perc'] = consol_table['btc_perc'][ticker]
        table[ticker]['fx_perc'] = consol_table['fx_perc'][ticker]
        table[ticker]['total_fees'] = consol_table['trade_fees'][ticker]
        table[ticker]['total_fees_fx'] = consol_table['trade_fees_fx'][ticker]

        table[ticker]['usd_price_data'] =\
            consol_table['price_data_USD'][ticker]
        try:
            table[ticker]['usd_price_data']['LASTUPDATE'] =\
                    (datetime.utcfromtimestamp(table[ticker]['usd_price_data']['LASTUPDATE']).strftime('%H:%M:%S'))
        except TypeError:
            table[ticker]['usd_price_data']['LASTUPDATE'] = 0
        table[ticker]['btc_price'] = consol_table['price_data_BTC'][ticker]
        table[ticker]['total_pnl_gross_USD'] =\
            consol_table['total_pnl_gross_USD'][ticker]
        table[ticker]['total_pnl_gross_fx'] =\
            consol_table['total_pnl_gross_fx'][ticker]
        table[ticker]['total_pnl_net_USD'] =\
            consol_table['total_pnl_net_USD'][ticker]
        table[ticker]['total_pnl_net_fx'] =\
            consol_table['total_pnl_net_fx'][ticker]
        table[ticker]['small_pos'] = consol_table['small_pos'][ticker]
        table[ticker]['cash_flow_value'] =\
            summary_table['cash_value'][ticker].to_dict()
        table[ticker]['cash_flow_value_fx'] =\
            summary_table['cash_value_fx'][ticker].to_dict()
        table[ticker]['trade_fees'] = \
            summary_table['trade_fees'][ticker].to_dict()
        table[ticker]['trade_fees_fx'] = \
            summary_table['trade_fees_fx'][ticker].to_dict()
        table[ticker]['trade_quantity'] = \
            summary_table['trade_quantity'][ticker].to_dict()
        table[ticker]['count'] = summary_table['count'][ticker].to_dict()
        table[ticker]['average_price'] = \
            summary_table['average_price'][ticker].to_dict()
        table[ticker]['average_price_fx'] = \
            summary_table['average_price_fx'][ticker].to_dict()

    return(table, pie_data)


@MWT(timeout=3)
def cleancsv(text):  # Function to clean CSV fields - leave only digits and .
    if text is None:
        return (0)
    acceptable = ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "."]
    str = ""
    for char in text:
        if char in acceptable:
            str = str + char
    str = float(str)
    return(str)


@timing
def generatenav(user, force=False, filter=None):
    logging.info(f"[generatenav] Starting NAV Generator for user {user}")
    # Portfolios smaller than this size do not account for NAV calculations
    # Otherwise, there's an impact of dust left in the portfolio (in USD)
    # This is set in config.ini file
    min_size_for_calc = int(PORTFOLIO_MIN_SIZE_NAV)
    logging.info(f"[generatenav] Force update status is {force}")
    save_nav = True
    # This process can take some time and it's intensive to run NAV
    # generation every time the NAV is needed. A compromise is to save
    # the last NAV generation locally and only refresh after a period of time.
    # This period of time is setup in config.ini as RENEW_NAV (in minutes).
    # If last file is newer than 60 minutes (default), the local saved file
    # will be used.
    # Unless force is true, then a rebuild is done regardless
    # Local files are  saved under a hash of username.
    if force:
        usernamehash = hashlib.sha256(current_user.username.encode(
            'utf-8')).hexdigest()
        filename = "thewarden/nav_data/"+usernamehash + ".nav"
        # Since this function can be run as a thread, it's safer to delete
        # the current NAV file if it exists. This avoids other tasks reading
        # the local file which is outdated
        try:
            os.remove(filename)
        except OSError:
            logging.info("[generatenav] Local NAV file was not found" +
                         " for removal - continuing")

    if not force:
        usernamehash = hashlib.sha256(current_user.username.encode(
            'utf-8')).hexdigest()
        filename = "thewarden/nav_data/"+usernamehash + ".nav"
        try:
            # Check if NAV saved file is recent enough to be used
            # Local file has to have a saved time less than RENEW_NAV min old
            # See config.ini to change RENEW_NAV
            modified = datetime.utcfromtimestamp(os.path.getmtime(filename))
            elapsed_seconds = (datetime.utcnow() - modified).total_seconds()
            logging.info(f"Last time file was modified {modified} is " +
                         f" {elapsed_seconds} seconds ago")
            if (elapsed_seconds/60) < int(RENEW_NAV):
                nav_pickle = pd.read_pickle(filename)
                return (nav_pickle)
            else:
                logging.info("File found but too old - rebuilding NAV")

        except FileNotFoundError:
            logging.info(f"[generatenav] File not found to load NAV" +
                         " - rebuilding")

    # Pandas dataframe with transactions
    df = transactions_fx()
    # if a filter argument was passed, execute it
    if filter:
        df = df.query(filter)
    logging.info("[generatenav] Success - read trades from database")
    start_date = df.index.min() - timedelta(days=1)  # start on t-1 of first trade
    end_date = datetime.today()

    # Create a list of all tickers that were traded in this portfolio
    tickers = df.trade_asset_ticker.unique().tolist()

    # Create an empty DF, fill with dates and fill with operation and prices then NAV
    dailynav = pd.DataFrame(columns=['date'])
    # Fill the dates from first trade until today
    dailynav['date'] = pd.date_range(start=start_date, end=end_date)
    dailynav = dailynav.set_index('date')
    # Create empty fields
    dailynav['PORT_usd_pos'] = 0
    dailynav['PORT_fx_pos'] = 0
    dailynav['PORT_cash_value'] = 0
    dailynav['PORT_cash_value_fx'] = 0

    # Create a dataframe for each position's prices
    for id in tickers:
        if is_currency(id):
            continue
        try:
            # Create a new PriceData class for this ticker
            prices = price_data_fx(id)
            if prices is None:
                logging.error(f"Could not get a price for {id}")
                save_nav = False
                raise ValueError

            prices = prices.rename(columns={'close_converted': id+'_price'})
            prices = prices[id+'_price']
            # Fill dailyNAV with prices for each ticker
            dailynav = pd.merge(dailynav, prices, on='date', how='left')

            # Replace NaN with prev value, if no prev value then zero
            dailynav[id+'_price'].fillna(method='ffill', inplace=True)
            dailynav[id+'_price'].fillna(0, inplace=True)

            # Now let's find trades for this ticker and include in dailynav
            tradedf = df[['trade_asset_ticker',
                          'trade_quantity', 'cash_value_fx']]
            # Filter trades only for this ticker
            tradedf = tradedf[tradedf['trade_asset_ticker'] == id]
            # consolidate all trades in a single date Input
            tradedf = tradedf.groupby(level=0).sum()
            tradedf.sort_index(ascending=True, inplace=True)
            # include column to cumsum quant
            tradedf['cum_quant'] = tradedf['trade_quantity'].cumsum()
            # merge with dailynav - 1st rename columns to match
            tradedf.index.rename('date', inplace=True)
            # rename columns to include ticker name so it's differentiated
            # when merged with other ids
            tradedf.rename(columns={'trade_quantity': id+'_quant',
                                    'cum_quant': id+'_pos',
                                    'cash_value_fx': id+'_cash_value_fx'},
                           inplace=True)
            # merge
            dailynav = pd.merge(dailynav, tradedf, on='date', how='left')
            # for empty days just trade quantity = 0, same for CV
            dailynav[id+'_quant'].fillna(0, inplace=True)
            dailynav[id+'_cash_value_fx'].fillna(0, inplace=True)
            # Now, for positions, fill with previous values, NOT zero,
            # unless there's no previous
            dailynav[id+'_pos'].fillna(method='ffill', inplace=True)
            dailynav[id+'_pos'].fillna(0, inplace=True)
            # Calculate USD and fx position and % of portfolio at date
            # Calculate USD position and % of portfolio at date
            dailynav[id+'_fx_pos'] = dailynav[id+'_price'].astype(
                float) * dailynav[id+'_pos'].astype(float)
            # Before calculating NAV, clean the df for small
            # dust positions. Otherwise, a portfolio close to zero but with
            # 10 sats for example, would still have NAV changes
            dailynav[id+'_fx_pos'].round(2)
            logging.info(
                f"Success: imported prices for id:{id}")
        except (FileNotFoundError, KeyError, ValueError) as e:
            logging.error(f"{id}: Error: {e}")
            flash(f"Ticker {id} generated an error. " +
                   f"NAV calculations will be off. Error: {e}", "danger")
    # Another loop to sum the portfolio values - maybe there is a way to
    # include this on the loop above. But this is not a huge time drag unless
    # there are too many tickers in a portfolio
    for id in tickers:
        if is_currency(id):
            continue
        # Include totals in new columns
        try:
            dailynav['PORT_fx_pos'] = dailynav['PORT_fx_pos'] +\
                dailynav[id+'_fx_pos']
        except KeyError as e:
            logging.error(f"[GENERATENAV] Ticker {id} was not found " +
                            "on NAV Table - continuing but this is not good." +
                            " NAV calculations will be erroneous.")
            save_nav = False
            flash(f"Ticker {id} was not found on NAV table. " +
                   f"NAV calculations will be off. Error: {e}", "danger")
            continue
        dailynav['PORT_cash_value_fx'] = dailynav['PORT_cash_value_fx'] +\
            dailynav[id+'_cash_value_fx']

    # Now that we have the full portfolio value each day, calculate alloc %
    for id in tickers:
        if is_currency(id):
            continue
        try:
            dailynav[id+"_fx_perc"] = dailynav[id+'_fx_pos'] /\
                dailynav['PORT_fx_pos']
            dailynav[id+"_fx_perc"].fillna(0, inplace=True)
        except KeyError:
            continue

    # Create a new column with the portfolio change only due to market move
    # discounting all cash flows for that day
    dailynav['adj_portfolio_fx'] = dailynav['PORT_fx_pos'] -\
        dailynav['PORT_cash_value_fx']

    # For the period return let's use the Modified Dietz Rate of return method
    # more info here: https://tinyurl.com/y474gy36
    # There is one caveat here. If end value is zero (i.e. portfolio fully
    # redeemed, the formula needs to be adjusted)
    dailynav.loc[dailynav.PORT_fx_pos > min_size_for_calc,
                 'port_dietz_ret_fx'] =\
        ((dailynav['PORT_fx_pos'] -
          dailynav['PORT_fx_pos'].shift(1)) -
         dailynav['PORT_cash_value_fx']) /\
        (dailynav['PORT_fx_pos'].shift(1) +
         abs(dailynav['PORT_cash_value_fx']))

    # Fill empty and NaN with zero
    dailynav['port_dietz_ret_fx'].fillna(0, inplace=True)
    dailynav['adj_port_chg_fx'] = ((dailynav['PORT_fx_pos'] -
                                    dailynav['PORT_fx_pos'].shift(1)) -
                                    dailynav['PORT_cash_value_fx'])

    # let's fill NaN with zeros
    dailynav['adj_port_chg_fx'].fillna(0, inplace=True)
    # Calculate the metrics
    dailynav['port_perc_factor_fx'] = (dailynav['port_dietz_ret_fx']) + 1
    dailynav['NAV_fx'] = dailynav['port_perc_factor_fx'].cumprod()
    dailynav['NAV_fx'] = dailynav['NAV_fx'] * 100
    dailynav['PORT_ac_CFs_fx'] = dailynav['PORT_cash_value_fx'].cumsum()
    logging.info(
        f"[generatenav] Success: NAV Generated for user {user}")

    # Save NAV Locally as Pickle
    if save_nav:
        usernamehash = hashlib.sha256(current_user.username.encode(
            'utf-8')).hexdigest()
        filename = "thewarden/nav_data/"+usernamehash + ".nav"
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        dailynav.to_pickle(filename)
        logging.info(f"[generatenav] NAV saved to {filename}")

    return dailynav


def regenerate_nav():
    # re-generates the NAV on the background - delete First
    # the local NAV file so it's not used.
    usernamehash = hashlib.sha256(
        current_user.username.encode("utf-8")).hexdigest()
    filename = "thewarden/nav_data/" + usernamehash + ".nav"
    logging.info(f"[newtrade] {filename} marked for deletion.")
    # Since this function can be run as a thread,
    # it's safer to delete the current NAV file if it exists.
    # This avoids other tasks reading the local file which
    # is outdated
    try:
        os.remove(filename)
        logging.info("[newtrade] Local NAV file deleted")
    except OSError:
        logging.info("[newtrade] Local NAV file not found" +
                     " for removal - continuing")
    # Delete all pricing history from AA
    aa_files = glob.glob('thewarden/alphavantage_data/*')
    [os.remove(x) for x in aa_files]
    nav_files = glob.glob('thewarden/dailydata/*.json')
    [os.remove(x) for x in nav_files]

    generatenav(current_user.username, True)
    logging.info("Change to database - generated new NAV")


def send_reset_email(user):
    token = user.get_reset_token()
    msg = Message('Password Reset Request',
                  sender='cryptoblotterrp@gmail.com',
                  recipients=[user.email])
    msg.body = f'''To reset your password, visit the following link:
                {url_for('users.reset_token', token=token, _external=True)}


                If you did not make this request then simply ignore this email
                 and no changes will be made.
                '''
    mail.send(msg)


@MWT(timeout=5)
def heatmap_generator():
    # If no Transactions for this user, return empty.html
    transactions = Trades.query.filter_by(user_id=current_user.username).order_by(
        Trades.trade_date
    )
    if transactions.count() == 0:
        return None, None, None, None

    # Generate NAV Table first
    data = generatenav(current_user.username)
    data["navpchange"] = (data["NAV_fx"] / data["NAV_fx"].shift(1)) - 1
    returns = data["navpchange"]
    # Run the mrh function to generate heapmap table
    heatmap = mrh.get(returns, eoy=True)

    heatmap_stats = heatmap
    cols = [
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
        "eoy",
    ]
    cols_months = [
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    ]
    years = (heatmap.index.tolist())
    heatmap_stats["MAX"] = heatmap_stats[heatmap_stats[cols_months] != 0].max(axis=1)
    heatmap_stats["MIN"] = heatmap_stats[heatmap_stats[cols_months] != 0].min(axis=1)
    heatmap_stats["POSITIVES"] = heatmap_stats[heatmap_stats[cols_months] > 0].count(
        axis=1
    )
    heatmap_stats["NEGATIVES"] = heatmap_stats[heatmap_stats[cols_months] < 0].count(
        axis=1
    )
    heatmap_stats["POS_MEAN"] = heatmap_stats[heatmap_stats[cols_months] > 0].mean(
        axis=1
    )
    heatmap_stats["NEG_MEAN"] = heatmap_stats[heatmap_stats[cols_months] < 0].mean(
        axis=1
    )
    heatmap_stats["MEAN"] = heatmap_stats[heatmap_stats[cols_months] != 0].mean(axis=1)

    return (heatmap, heatmap_stats, years, cols)


@memoized
def fx_list():
    with open('thewarden/static/csv_files/physical_currency_list.csv', newline='') as csvfile:
        reader = csv.reader(csvfile)
        fiat_dict = {rows[0]: (rows[1]) for rows in reader}
    # Convert dict to list
    fx_list = [(k, k + ' | ' + v) for k, v in fiat_dict.items()]
    fx_list.sort()
    return (fx_list)


@MWT(timeout=5)
def fxsymbol(fx, output='symbol'):
    # Gets an FX 3 letter symbol and returns the HTML symbol
    # Sample outputs are:
    # "EUR": {
    # "symbol": "€",
    # "name": "Euro",
    # "symbol_native": "€",
    # "decimal_digits": 2,
    # "rounding": 0,
    # "code": "EUR",
    # "name_plural": "euros"
    with open('thewarden/static/json_files/currency.json') as fx_json:
        fx_list = json.load(fx_json)
    try:
        out = fx_list[fx][output]
    except Exception:
        out = fx
    return (out)


# ------------------------------------
# Helpers for Bitmex start here
# ------------------------------------

def bitmex_gethistory(ticker):
    # Gets historical prices from bitmex
    # Saves to folder
    from bitmex import bitmex
    testnet = False
    logging.info(f"[Bitmex] Trying Bitmex for {ticker}")
    bitmex_credentials = load_bitmex_json()

    if ("api_key" in bitmex_credentials) and ("api_secret" in bitmex_credentials):
        try:
            mex = bitmex(test=testnet,
                         api_key=bitmex_credentials['api_key'],
                         api_secret=bitmex_credentials['api_secret'])
            # Need to paginate results here to get all the history
            # Bitmex API end point limits 750 results per call
            start_bin = 0
            resp = (mex.Trade.Trade_getBucketed(
                symbol=ticker, binSize="1d", count=750, start=start_bin).result())[0]
            df = pd.DataFrame(resp)
            last_update = df['timestamp'].iloc[-1]

            # If last_update is older than 3 days ago, keep building.
            while last_update < (datetime.now(timezone.utc) - timedelta(days=3)):
                start_bin += 750
                resp = (mex.Trade.Trade_getBucketed(
                    symbol=ticker, binSize="1d", count=750, start=start_bin).result())[0]
                df = df.append(resp)
                last_update = df['timestamp'].iloc[-1]
                # To avoid an infinite loop, check if start_bin
                # is higher than 10000 and stop (i.e. 30+yrs of data)
                if start_bin > 10000:
                    logging.error("[Bitmex] Something went wrong on price grab loop. Forced quit of loop.")
                    break

            logging.info(f"[Bitmex] Success. Downloaded data for {ticker}")
            return(df)

        except Exception as e:
            logging.error(f"[Bitmex] error: {e}")
            return ("error")

    else:
        logging.warning(f"[Bitmex] No Credentials Found")
        return ('error')


def save_bitmex_json(api_key, api_secret):
    # receives api_key and api_secret then saves to a local json for later use
    if (api_key is None) or (api_secret is None):
        return ("missing arguments")
    bitmex_data = {"api_key": api_key, "api_secret": api_secret}
    with open('thewarden/api/bitmex.json', 'w') as fp:
        json.dump(bitmex_data, fp)
        return ("Credentials saved to bitmex.json")


def load_bitmex_json():
    # returns current stored keys if any
    try:
        with open('thewarden/api/bitmex.json', 'r') as fp:
            data = json.load(fp)
            return (data)
    except (FileNotFoundError, KeyError):
        return ({'status': 'error', 'message': 'API credentials not found'})


def bitmex_orders(api_key, api_secret, testnet=True):
    # Returns a json with all Bitmex Order History
    # Takes arguments: ticker, testnet
    # reads api credentials from file
    from bitmex import bitmex
    try:
        mex = bitmex(test=testnet, api_key=api_key, api_secret=api_secret)
    except Exception:
        return ("Connection Error. Check your connection.")
    try:
        resp = mex.Execution.Execution_getTradeHistory(count=500, start=0, reverse=True).result()
        # resp = mex.User.User_getWalletHistory(count=50000).result()
        # resp = mex.User.User_getWalletHistory(currency=ticker, count=5000).result()
    except Exception:
        resp = "Invalid Credential or Connection Error"
    return(resp)
