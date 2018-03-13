# Copyright 2017 QuantRocket LLC - All Rights Reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import io
import pandas as pd
import numpy as np
from moonshot.slippage import FixedSlippage
from moonshot.mixins import (
    WeightAllocationMixin,
    LiquidityConstraintMixin,
    ReutersFundamentalsMixin
)
from quantrocket.history import download_history_file, get_db_config
from quantrocket.master import download_master_file
from quantrocket.account import download_account_balances, download_exchange_rates
from quantrocket.blotter import list_positions

class Moonshot(
    LiquidityConstraintMixin,
    WeightAllocationMixin,
    ReutersFundamentalsMixin):
    """
    Base class for Moonshot strategies.

    To create a strategy, subclass this class. Implement your trading logic in the class
    methods, and store your strategy parameters as class attributes.

    Class attributes include built-in Moonshot parameters which you can override, as well
    as your own custom parameters.

    To run a backtest, at minimum you must implement `get_signals`, but in general you will
    want to implement the following methods (which are called in the order shown):

        `get_signals` -> `allocate_weights` -> `simulate_positions` -> `simulate_gross_returns`

    To trade (i.e. generate orders intended to be placed, but actually placed by other services
    than Moonshot), you must also implement `create_orders`. Order generation for trading
    follows the path shown below:

        `get_signals` -> `allocate_weights` -> `create_orders`

    Parameters
    ----------
    CODE : str, required
        the strategy code

    DB : str, required
        code of history db to pull data from

    DB_FIELDS : list of str, optional
        fields to retrieve from history db (defaults to ["Open", "High", "Low",
        "Close", "Volume"])

    CONIDS : list of int, optional
        limit history db query to these conids

    UNIVERSES : list of str, optional
        limit history db query to these universes

    EXCLUDE_CONIDS : list of int, optional
        exclude these conids from history db query

    EXCLUDE_UNIVERSES : list of str, optional
        exclude these universes from history db query

    CONT_FUT : str, optional
        pass this cont_fut option to history db query (default None)

    LOOKBACK_WINDOW : int, optional
        get this much additional data prior to the backtest start date or trade date
        to account for rolling windows (default 0)

    MASTER_FIELDS : list of str, optional
        get these fields from the securities master service (defaults to ["Currency",
        "MinTick", "Multiplier", "PriceMagnifier", "PrimaryExchange", "SecType", "Symbol",
        "Timezone"])

    NLV : dict, optional
        dict of currency:NLV for each currency represented in the strategy. Can
        alternatively be passed directly to backtest method.

    COMMISSION_CLASS : Class or dict of (sectype,exchange,currency):Class, optional
        the commission class to use. If strategy includes a mix of security types,
        exchanges, or currencies, you can pass a dict mapping tuples of
        (sectype,exchange,currency) to the different commission classes. By default
        no commission is applied.

    SLIPPAGE_CLASSES : iterable of slippage classes
        one or more slippage classes. By default no slippage is applied.

    SLIPPAGE_BPS : float, optional
        amount on one-slippage to apply to each trade in BPS (for example, enter 5 to deduct
        5 BPS)

    BENCHMARK : int, optional
        the conid of a security in the historical data to use as the benchmark

    Examples
    --------
    Example of a minimal strategy that runs on a history db called "mexi-stk" and buys when
    the securities are above their 200-day moving average:

    >>> MexicoMovingAverage(Moonshot):
    >>>
    >>>     CODE = "mexi-ma"
    >>>     DB = "mexi-stk"
    >>>     MAVG_WINDOW = 200
    >>>
    >>>     def get_signals(self, prices):
    >>>         closes = prices.loc["Close"]
    >>>         mavgs = closes.rolling(self.MAVG_WINDOW).mean()
    >>>         signals = closes > mavgs.shift()
    >>>         return signals.astype(int)
    """
    CODE = None
    DB = None
    DB_FIELDS = ["Open", "High", "Low", "Close", "Volume"]
    DB_TIME_FILTERS = None
    CONIDS = None
    UNIVERSES = None
    EXCLUDE_CONIDS = None
    EXCLUDE_UNIVERSES = None
    CONT_FUT = None
    LOOKBACK_WINDOW = 0
    MASTER_FIELDS = [
        "Currency", "MinTick", "Multiplier", "PriceMagnifier",
        "PrimaryExchange", "SecType", "Symbol", "Timezone"]
    NLV = None
    QUANTITY_CALCULATOR = None
    COMMISSION_CLASS = None
    SLIPPAGE_CLASSES = ()
    SLIPPAGE_BPS = 0
    BENCHMARK = None

    def __init__(self):
        self.is_trade = False
        self.is_backtest = False

    def get_signals(self, prices):
        """
        Return a DataFrame of signals based on the prices. By convention,
        signals should be 1=long, 0=cash, -1=short.

        Must be implemented by strategy subclasses.

        Parameters
        ----------
        prices : DataFrame, required
            multiindex (Field, Date) DataFrame of price/market data

        Returns
        -------
        DataFrame
            signals

        Examples
        --------
        Buy when the close is above yesterday's 50-day moving average:

        >>> def get_signals(self, prices):
        >>>     closes = prices.loc["Close"]
        >>>     mavgs = closes.rolling(50).mean()
        >>>     signals = closes > mavgs.shift()
        >>>     return signals.astype(int)
        """
        raise NotImplementedError("strategies must implement get_signals")

    def allocate_weights(self, signals, prices):
        """
        Return a DataFrame of weights based on the signals.

        Whereas signals indicate the direction of the trades, weights
        indicate both the direction and size. For example, -0.5 means a short
        position equal to 50% of the equity allocated to the strategy.

        Weights are used to help create orders in live trading, and to help
        simulate executed positions in backtests.

        The default implemention of this method evenly divides allocated
        capital among the signals each period, but it is intended to be
        overridden by strategy subclasses.

        A variety of built-in weight allocation algorithms are provided by
        and documented under `moonshot.mixins.WeightAllocationMixin`.

        Parameters
        ----------
        signals : DataFrame, required
            a DataFrame of signals

        prices : DataFrame, required
            multiindex (Field, Date) DataFrame of price/market data

        Returns
        -------
        DataFrame
            weights

        Examples
        --------
        The default implementation is shown below:

        >>> def allocate_weights(self, signals, prices):
        >>>     weights = self.allocate_equal_weights(signals) # provided by moonshot.mixins.WeightAllocationMixin
        >>>     return weights
        """
        weights = self.allocate_equal_weights(signals)
        return weights

    def simulate_positions(self, weights, prices):
        """
        Return a DataFrame of simulated positions based on the allocated
        weights.

        The positions should shift the weights based on when the weights
        would be filled in live trading.

        By default, assumes the position are taken in the period after the
        weights were allocated. Intended to be overridden by strategy
        subclasses.

        Parameters
        ----------
        weights : DataFrame, required
            a DataFrame of weights

        prices : DataFrame, required
            multiindex (Field, Date) DataFrame of price/market data

        Returns
        -------
        DataFrame
            positions

        Examples
        --------
        The default implemention is shown below (enter position in the period after
        signal generation/weight allocation):

        >>> def simulate_positions(self, weights, prices):
        >>>     positions = weights.shift()
        >>>     return positions
        """
        positions = weights.shift()
        return positions

    def simulate_gross_returns(self, positions, prices):
        """
        Returns a DataFrame of returns before commissions and slippage.

        By default, assumes entry on the close on the period the position is
        taken and calculates the return through the following period's close.
        Intended to be overridden by strategy subclasses.

        Parameters
        ----------
        positions : DataFrame, required
            a DataFrame of positions

        prices : DataFrame, required
            multiindex (Field, Date) DataFrame of price/market data

        Returns
        -------
        DataFrame
            gross returns

        Examples
        --------
        The default implementation is shown below:

        >>> def simulate_gross_returns(self, positions, prices):
        >>>     closes = prices.loc["Close"]
        >>>     gross_returns = closes.pct_change() * positions.shift()
        >>>     return gross_returns
        """
        closes = prices.loc["Close"]
        gross_returns = closes.pct_change() * positions.shift()
        return gross_returns

    def create_orders(self, orders, prices):
        """
        From a DataFrame of order stubs, creates a DataFrame of fully
        specified orders.

        Parameters
        ----------
        orders : DataFrame
            a DataFrame of order stubs, with columns ConId, Account, Action,
            OrderRef, and TotalQuantity

        prices : DataFrame
            multiindex (Field, Date) DataFrame of price/market data

        Returns
        -------
        DataFrame
            a DataFrame of fully specified orders, with (at minimum) columns
            Exchange, Tif, OrderType added

        Examples
        --------
        The orders DataFrame provided to this method resembles the following:

        >>> print(orders)
            ConId  Account Action     OrderRef  TotalQuantity
        0   12345   U12345   SELL  my-strategy            100
        1   12345   U55555   SELL  my-strategy             50
        2   23456   U12345    BUY  my-strategy            100
        3   23456   U55555    BUY  my-strategy             50
        4   34567   U12345    BUY  my-strategy            200
        5   34567   U55555    BUY  my-strategy            100

        The default implemention creates MKT DAY orders routed to SMART and is
        shown below:

        >>> def create_orders(self, orders, prices):
        >>>     orders["Exchange"] = "SMART"
        >>>     orders["OrderType"] = "MKT"
        >>>     orders["Tif"] = "DAY"
        >>>     return orders

        Set a limit price equal to the prior closing price:

        >>> closes = prices.loc["Close"]
        >>> prior_closes = closes.shift()
        >>> prior_closes = self.reindex_like_orders(prior_closes, orders)
        >>> orders["OrderType"] = "LMT"
        >>> orders["LmtPrice"] = prior_closes
        """
        orders["Exchange"] = "SMART"
        orders["OrderType"] = "MKT"
        orders["Tif"] = "DAY"
        return orders

    def reindex_like_orders(self, df, orders):
        """
        Reindexes a DataFrame (having ConIds as columns and dates as index)
        to match the shape of the orders DataFrame.

        Parameters
        ----------
        df : DataFrame, required
            a DataFrame of arbitrary values with ConIds as columns and
            dates as index

        orders : DataFrame, required
            an orders DataFrame with a ConId column

        Returns
        -------
        Series
            a Series with an index matching orders

        Examples
        --------
        Calculate prior closes and reindex like orders:

        >>> closes = prices.loc["Close"]
        >>> prior_closes = closes.shift()
        >>> prior_closes = self.reindex_like_orders(prior_closes, orders)
        """
        signal_date = self._get_signal_date(df.index)
        df = df.loc[signal_date]
        df.name = "_MoonshotOther"
        df = orders.join(df, on="ConId")._MoonshotOther
        df.name = None
        return df

    def _create_order_stubs(self, quantities):
        """
        From a DataFrame of quantities to be ordered (with ConIds as index,
        Accounts as columns), returns a DataFrame of order stubs.

        quantities in:

        Account   U12345  U55555
        ConId
        12345       -100     -50
        23456        100      50
        34567        200     100

        order_stubs out:

            ConId  Account Action     OrderRef  TotalQuantity
        0   12345   U12345   SELL  my-strategy            100
        1   12345   U55555   SELL  my-strategy             50
        2   23456   U12345    BUY  my-strategy            100
        3   23456   U55555    BUY  my-strategy             50
        4   34567   U12345    BUY  my-strategy            200
        5   34567   U55555    BUY  my-strategy            100

        """
        quantities.index.name = "ConId"
        quantities.columns.name = "Account"
        quantities = quantities.stack()
        quantities.name = "Quantity"
        order_stubs = quantities.to_frame().reset_index()
        order_stubs["Action"] = np.where(order_stubs.Quantity > 0, "BUY", "SELL")
        order_stubs = order_stubs.loc[order_stubs.Quantity != 0].copy()
        order_stubs["OrderRef"] = self.CODE
        order_stubs["TotalQuantity"] = order_stubs.Quantity.abs()
        order_stubs = order_stubs.drop("Quantity",axis=1)

        return order_stubs

    def _get_nlv(self):
        """
        Return a dict of currency:NLV for each currency in the strategy. By
        default simply returns the NLV class attribute.
        """
        return self.NLV

    def _get_trades(self, positions):
        """
        Given a dataframe of positions, returns a dataframe of trades. 0
        indicates no trade; 1 indicates going from 100% short to cash or cash
        to 100% long, and vice versa; and 2 indicates going from 100% short
        to %100 long. Fractional positions can result in fractional trades.
        """
        trades = positions.diff()
        return trades

    def _get_returns(self, positions, prices):
        """
        Returns a DataFrame of 1-period returns, after commissions and slippage.
        """
        gross_returns = self.simulate_gross_returns(positions, prices)
        commissions = self._get_commissions(positions, prices)
        slippage = self._get_slippage(positions, prices)

        returns = gross_returns - commissions - slippage
        return returns

    def _get_signal_date(self, index):
        """
        Returns the index value that should be used for today's trading. By
        default this the last date.
        """
        return index[-1]

    def _get_commissions(self, positions, prices):
        """
        Returns the commissions to be subtracted from the returns.
        """
        if not self.COMMISSION_CLASS:
            return pd.DataFrame(0, index=positions.index, columns=positions.columns)

        trades = self._get_trades(positions)
        contract_values = self._get_contract_values(prices)

        fields = prices.index.get_level_values("Field").unique()
        if "Nlv" in fields:
            nlvs = prices.loc["Nlv"]
        else:
            nlvs = None

        # handle the case of only one commission class
        if not isinstance(self.COMMISSION_CLASS, dict):
            commissions = self.COMMISSION_CLASS.get_commissions(contract_values, trades=trades, nlvs=nlvs)
            return commissions

        # handle multiple commission classes per sectype/exchange/currency

        # first, tuple-ize the dict keys in case they are lists
        commission_classes = {}
        for sec_group, commission_cls in self.COMMISSION_CLASS.items():
            commission_classes[tuple(sec_group)] = commission_cls

        defined_sec_groups = set([tuple(k) for k in commission_classes.keys()])
        sec_types = prices.loc["SecType"]
        exchanges = prices.loc["PrimaryExchange"]
        currencies = prices.loc["Currency"]
        required_sec_groups = set([
            tuple(s.split("|")) for s in (sec_types+"|"+exchanges+"|"+currencies).iloc[-1].unique()])
        missing_sec_groups = required_sec_groups - defined_sec_groups
        if missing_sec_groups:
            raise ValueError("expected a commission class for each combination of (sectype,exchange,currency) "
                             "but none is defined for {0}".format(
                                 ", ".join([str(m) for m in missing_sec_groups])))

        all_commissions = pd.DataFrame(None, index=positions.index, columns=positions.columns)

        for sec_group in required_sec_groups:
            commission_cls = commission_classes[sec_group]
            sec_type, exchange, currency = sec_group

            sec_group_commissions = commission_cls.get_commissions(
                contract_values, trades=trades, nlvs=nlvs)

            in_sec_group = (sec_types == sec_type) & (exchanges == exchange) & (currencies == currency)
            all_commissions = sec_group_commissions.where(in_sec_group, all_commissions)

        return all_commissions

    def _get_slippage(self, positions, prices):
        """
        Returns the slippage to be subtracted from the returns.
        """
        trades = self._get_trades(positions)
        slippage = pd.DataFrame(0, index=trades.index, columns=trades.columns)

        for slippage_class in self.SLIPPAGE_CLASSES:
            slippage += slippage_class().get_slippage(trades, positions, prices)

        if self.SLIPPAGE_BPS:
            slippage = FixedSlippage(self.SLIPPAGE_BPS/10000.0).get_slippage(trades, positions, prices)

        return slippage

    def _constrain_weights(self, weights, prices):
        """
        Constrains the weights by the max and min allowed quantities (as
        dictated by available liquidity, contract size, etc.).
        """
        max_allowed_quantities = self.get_max_allowed_quantities(prices)
        min_allowed_quantities = self.get_min_allowed_quantities(prices)

        if max_allowed_quantities is None and min_allowed_quantities is None:
            # no constraints
            return weights

        if "Nlv" not in prices.index.get_level_values("Field").unique():
            raise ValueError("must provide NLVs to constrain weights")

        target_trade_values = weights.abs() * prices.loc["Nlv"]
        contract_values = self._get_contract_values(prices)
        target_quantities = target_trade_values / contract_values.shift()

        if max_allowed_quantities is None:
            max_allowed_quantities = target_quantities

        if min_allowed_quantities is None:
            min_allowed_quantities = target_quantities

        # Get trades because we only constrain weights if we're entering a trade
        trades = self._get_trades(weights)

        reduce_weights = ((target_quantities > max_allowed_quantities) & (trades.abs() > 0)).fillna(False)
        weights = weights.where(reduce_weights == False, weights * max_allowed_quantities/target_quantities.replace(0, 1))

        increase_weights = ((target_quantities < min_allowed_quantities) & (trades.abs() > 0)).fillna(False)
        weights = weights.where(increase_weights == False, weights * min_allowed_quantities/target_quantities.replace(0, 1))

        return weights

    def get_max_allowed_quantities(self, prices):
        """
        Return a DataFrame of max allowed quantities based on constraints
        such as available liquidity.

        This method is a hook for subclasses. Return None to indicate no
        constraint.

        Parameters
        ----------
        prices : DataFrame, required
            multiindex (Field, Date) DataFrame of price/market data

        Returns
        -------
        DataFrame
            quantities
        """
        return None

    def get_min_allowed_quantities(self, prices):
        """
        Returns a DataFrame of min allowed quantities based on constraints
        such as contract size, etc.

        This method is a hook for subclasses. Return None to indicate no
        constraint.

        Parameters
        ----------
        prices : DataFrame, required
            multiindex (Field, Date) DataFrame of price/market data

        Returns
        -------
        DataFrame
            quantities
        """
        return None

    @classmethod
    def _get_start_date_with_lookback(cls, start_date):
        """
        Returns the start_date adjusted to incorporate the LOOKBACK_WINDOW,
        plus a buffer. LOOKBACK_WINDOW is measured in trading days, but we
        query the db in calendar days. Convert from weekdays (260 per year)
        to calendar days, assuming 25 holidays (NYSE has ~9 per year, TSEJ
        has ~19), plus a 10 day buffer to be safe.
        """

        start_date = pd.Timestamp(start_date) - pd.Timedelta(
            days=cls.LOOKBACK_WINDOW*365.0/(260 - 25) + 10)
        return start_date.date().isoformat()

    def _get_historical_prices(self, start_date, end_date=None, nlv=None):
        """
        Downloads historical prices from a history db. Downloads security
        details from the master db and broadcasts the values to be shaped
        like the historical prices.
        """
        if start_date:
            start_date = self._get_start_date_with_lookback(start_date)

        f = io.StringIO()
        download_history_file(
            self.DB, f,
            start_date=start_date,
            end_date=end_date,
            universes=self.UNIVERSES,
            conids=self.CONIDS,
            exclude_universes=self.EXCLUDE_UNIVERSES,
            exclude_conids=self.EXCLUDE_CONIDS,
            times=self.DB_TIME_FILTERS,
            cont_fut=self.CONT_FUT,
            fields=self.DB_FIELDS,
            tz_naive=False)

        prices = pd.read_csv(f)

        prices = prices.pivot(index="ConId", columns="Date").T
        prices.index.set_names(["Field", "Date"], inplace=True)

        dates = pd.to_datetime(prices.index.get_level_values("Date"))
        dates = dates.tz_localize("UTC")
        prices.index = pd.MultiIndex.from_arrays((
            prices.index.get_level_values("Field"),
            dates
        ), names=("Field", "Date"))

        # Next, get the master file
        universes = self.UNIVERSES
        conids = self.CONIDS
        if not conids and not universes:
            universes = get_db_config(self.DB).get("universes", None)
            if not universes:
                conids = list(prices.columns)

        f = io.StringIO()
        download_master_file(
            f,
            conids=conids,
            universes=universes,
            exclude_conids=self.EXCLUDE_CONIDS,
            exclude_universes=self.EXCLUDE_UNIVERSES,
            fields=self.MASTER_FIELDS
        )
        securities = pd.read_csv(f, index_col="ConId")

        nlv = nlv or self._get_nlv()
        if nlv:
            missing_nlvs = set(securities.Currency) - set(nlv.keys())
            if missing_nlvs:
                raise ValueError(
                    "NLV dict is missing values for required currencies: {0}".format(
                        ", ".join(missing_nlvs)))

            securities['Nlv'] = securities.apply(lambda row: nlv.get(row.Currency, None), axis=1)

        securities = pd.DataFrame(securities.T, columns=prices.columns)
        securities.index.name = "Field"
        idx = pd.MultiIndex.from_product(
            (securities.index, prices.index.get_level_values("Date").unique()),
            names=["Field", "Date"])

        broadcast_securities = securities.reindex(index=idx, level="Field")
        prices = pd.concat((prices, broadcast_securities))

        return prices

    def backtest(self, start_date=None, end_date=None, nlv=None, allocation=1.0):
        """
        Backtest a strategy and return a DataFrame of results.

        Typically you'll run backtests via the QuantRocket client and won't
        call this method directly.

        Parameters
        ----------
        start_date : str (YYYY-MM-DD), optional
            the backtest start date (default is to include all history in db)

        end_date : str (YYYY-MM-DD), optional
            the backtest end date (default is to include all history in db)

        nlv : dict
            dict of currency:nlv. Should contain a currency:nlv pair for
            each currency represented in the strategy

        allocation : float
            how much to allocate to the strategy

        Returns
        -------
        DataFrame
            multiindex (Field, Date) DataFrame of backtest results
        """
        self.is_backtest = True
        allocation = allocation or 1.0

        prices = self._get_historical_prices(start_date, end_date, nlv=nlv)

        signals = self.get_signals(prices)
        weights = self.allocate_weights(signals, prices)
        weights = weights * allocation
        weights = self._constrain_weights(weights, prices)
        positions = self.simulate_positions(weights, prices)
        returns = self._get_returns(positions, prices)
        trades = self._get_trades(positions)
        commissions = self._get_commissions(positions, prices)

        backtest_results = pd.concat(
            dict(
                Signal=signals,
                Weight=weights,
                Position=positions,
                Trade=trades,
                Commission=commissions,
                Return=returns),
            names=["Field","Date"])

        results = pd.concat((backtest_results, prices))

        # truncate at requested start_date
        if start_date:
            results = results.iloc[
                results.index.get_level_values("Date") >= pd.Timestamp(start_date)]

        return results

    def trade(self, allocations):
        """
        Run the strategy and create orders.

        Parameters
        ----------
        allocations : dict, required
            dict of account:allocation to strategy (expressed as a percentage of NLV)

        Returns
        -------
        DataFrame
            orders
        """
        self.is_trade = True

        start_date = pd.Timestamp.today()

        prices = self._get_historical_prices(start_date)

        signals = self.get_signals(prices)

        signal_date = self._get_signal_date(signals.index)

        weights = self.allocate_weights(signals, prices)

        # get the latest date's weights; this results in a Series of weights by ConId:
        # ConId
        # 12345  -0.2
        # 23456     0
        # 34567   0.1
        weights = weights.loc[signal_date]

        allocations = pd.Series(allocations)
        # Out:
        # U12345    0.25
        # U55555    0.50

        # Multiply weights times allocations
        weights = weights.apply(lambda x: x * allocations)

        # Out:
        #        U12345  U55555
        # 12345  -0.050   -0.10
        # 23456   0.000    0.00
        # 34567   0.025    0.05

        contract_values = self._get_contract_values(prices)
        contract_values = contract_values.fillna(method="ffill").loc[signal_date]
        contract_values = allocations.apply(lambda x: contract_values).T
        # Out:
        #          U12345    U55555
        # 12345     95.68     95.68
        # 23456   1500.00   1500.00
        # 34567   3600.00   3600.00

        currencies = prices.loc["Currency"].loc[signal_date]
        sec_types = prices.loc["SecType"].loc[signal_date]

        # For FX, exchange rate conversions should be based on the symbol,
        # not the currency (i.e. 100 EUR.USD = 100 EUR, not 100 USD)
        if (sec_types == "CASH").any():
            symbols = prices.loc["Symbol"].loc[signal_date]
            currencies = currencies.where(sec_types != "CASH", symbols)

        f = io.StringIO()
        download_account_balances(
            f,
            latest=True,
            accounts=list(allocations.index),
            fields=["NetLiquidation"])

        balances = pd.read_csv(f, index_col="Account")

        f = io.StringIO()
        download_exchange_rates(
            f, latest=True,
            base_currencies=list(balances.Currency.unique()),
            quote_currencies=list(currencies.unique()))
        exchange_rates = pd.read_csv(f)

        nlvs = balances.NetLiquidation.reindex(allocations.index)
        # Out:
        # U12345 1000000
        # U55555  500000

        nlvs = weights.apply(lambda x: nlvs, axis=1)
        # Out:
        #        U12345 U55555
        # 12345 1000000 500000
        # 23456 1000000 500000
        # 34567 1000000 500000

        base_currencies = balances.Currency.reindex(allocations.index)
        # Out:
        # U12345 USD
        # U55555 EUR

        base_currencies = weights.apply(lambda x: base_currencies, axis=1)
        # Out:
        #        U12345 U55555
        # 12345     USD    EUR
        # 23456     USD    EUR
        # 34567     USD    EUR

        trade_currencies = allocations.apply(lambda x: currencies).T
        # Out:
        #        U12345 U55555
        # 12345     USD    USD
        # 23456     JPY    JPY
        # 34567     JPY    JPY

        base_currencies = base_currencies.stack()
        trade_currencies= trade_currencies.stack()
        base_currencies.name = "BaseCurrency"
        trade_currencies.name = "QuoteCurrency"
        currencies = pd.concat((base_currencies,trade_currencies), axis=1)
        # Out:
        #              BaseCurrency QuoteCurrency
        # 12345 U12345          USD           USD
        #       U55555          EUR           USD
        # 23456 U12345          USD           JPY
        #       U55555          EUR           JPY
        # 34567 U12345          USD           JPY
        #       U55555          EUR           JPY

        exchange_rates = pd.merge(currencies, exchange_rates, how="left",
                                  on=["BaseCurrency","QuoteCurrency"])
        exchange_rates.index = currencies.index
        exchange_rates.loc[(exchange_rates.BaseCurrency == exchange_rates.QuoteCurrency), "Rate"] = 1
        exchange_rates = exchange_rates.Rate.unstack()
        # Out:
        #        U12345  U55555
        # 12345    1.00    1.15
        # 23456  107.02  127.12
        # 34567  107.02  127.12

        target_quantities = self._get_target_quantities(
            weights, nlvs, exchange_rates, contract_values)

        positions = list_positions(
            order_refs=[self.CODE],
            accounts=list(allocations.index),
            conids=list(target_quantities.index)
        )

        if not positions:
            net_quantities = target_quantities
        else:
            positions = pd.DataFrame(positions)
            positions = positions.set_index(["ConId","Account"]).Quantity
            target_quantities = target_quantities.stack()
            target_quantities.index.set_names(["ConId","Account"], inplace=True)
            positions = positions.reindex(target_quantities.index).fillna(0)
            net_quantities = target_quantities - positions
            net_quantities = net_quantities.unstack()

        if (net_quantities == 0).all().all():
            return

        # contrain quantities
        max_allowed_quantities = self.get_max_allowed_quantities(prices)
        if max_allowed_quantities is not None:
            max_allowed_quantities = max_allowed_quantities.loc[signal_date]
            max_allowed_quantities = allocations.apply(lambda x: max_allowed_quantities).T
            net_quantities = net_quantities.where(
                net_quantities.abs() <= max_allowed_quantities,
                max_allowed_quantities.where(net_quantities > 0, -max_allowed_quantities))

        min_allowed_quantities = self.get_min_allowed_quantities(prices)
        if min_allowed_quantities is not None:
            min_allowed_quantities = min_allowed_quantities.loc[signal_date]
            min_allowed_quantities = allocations.apply(lambda x: min_allowed_quantities).T
            net_quantities = net_quantities.where(
                net_quantities.abs() >= min_allowed_quantities,
                min_allowed_quantities.where(net_quantities > 0, -min_allowed_quantities))

        order_stubs = self._create_order_stubs(net_quantities)
        orders = self.create_orders(order_stubs, prices)

        # TODO: where/how is price rounding handled?
        return orders

    def _get_target_quantities(self, weights, nlvs, exchange_rates, contract_values):
        """
        Converts a DataFrame of target percentage weights to a DataFrame of
        target quantities.

        Parameters
        ----------
        weights : DataFrame, required
            DataFrame of target percentage weights

        nlvs : DataFrame, required
            DataFrame of net liquidation values

        exchange_rates : DataFrame, required
            DataFrame of exchange rates

        contract_values : DataFrame, required
            DataFrame of contract values (price / magnifier * multiplier)

        """
        target_trade_values_in_base_currency = weights * nlvs
        target_trade_values_in_trade_currency = target_trade_values_in_base_currency * exchange_rates
        target_quantities = target_trade_values_in_trade_currency / contract_values
        return target_quantities.round().fillna(0).astype(int)

    def _get_contract_values(self, prices):
        """
        Return a DataFrame of contract values by multiplying prices times
        multipliers and dividing by price magnifiers.
        """
        # Find a price field we can use
        field = None
        fields = prices.index.get_level_values("Field").unique()
        candidate_fields = ("Close", "Open", "Bid", "Ask", "High", "Low")
        for candidate in candidate_fields:
            if candidate in fields:
                field = candidate
                break
        else:
            raise ValueError("Can't calculate contract values without one of {0}".format(
                ", ".join(candidate_fields)))

        closes = prices.loc[field]
        price_magnifiers = prices.loc["PriceMagnifier"]
        multipliers = prices.loc["Multiplier"]
        contract_values = closes / price_magnifiers.fillna(1) * multipliers.fillna(1)
        return contract_values