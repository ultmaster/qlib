# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple, TypedDict

import numpy as np
import pandas as pd

from qlib.backtest import Order
from qlib.backtest.decision import OrderDir
from qlib.constant import EPS
from qlib.rl.simulator import Simulator
from qlib.rl.tasks.data.pickle_styled import (
    IntradayBacktestData, get_intraday_backtest_data, DealPriceType
)

__all__ = ['SAOEMetrics', 'SAOEState', 'SingleAssetOrderExecution']

ONE_SEC = pd.Timedelta('1s')  # use 1 second to exclude the right interval point



class SAOEMetrics(TypedDict):
    """Metrics for SAOE accumulated for a "period".
    It could be accumulated for a day, or a period of time (e.g., 30min), or calculated separately for every minute.
    """

    datetime: pd.Timestamp              # datetime of this record (this is index in the dataframe)

    # Market information.
    market_volume: float                # (total) market volume traded in the period
    market_price: float                 # deal price. If it's a period of time, this is the average market deal price

    # Strategy records.
    amount: float                       # total amount (volume) strategy intends to trade
    inner_amount: float                 # total amount that the lower-level strategy intends to trade
                                        # (might be larger than amount, e.g., to ensure ffr)
    deal_amount: float                  # amount that successfully takes effect (must be less than inner_amount)
    trade_price: float                  # the average deal price for this strategy
    trade_value: float                  # total worth of trading. In the simple simulaton, trade_value = deal_amount * price
    position: float                     # Position left after this "period".

    # Accumulated metrics
    ffr: float                          # complete how much percent of the daily order
    pa: float                           # price advantage compared to baseline (i.e., trade with baseline market price)
                                        # the baseline is average market price of **all day** (there could be data leak here)
                                        # unit is BP (basis point, 1/10000)


class SAOEState(NamedTuple):
    """Data structure holding a state for SAOE simulator."""
    order: Order                        # the order we are dealing with
    cur_time: pd.Timestamp              # current time, e.g., 9:30
    position: float                     # current remaining volume to execute
    history_exec: pd.DataFrame          # see :attr:`SingleAssetOrderExecution.history_exec`
    history_steps: pd.DataFrame         # see :attr:`SingleAssetOrderExecution.history_steps`

    metric: SAOEState                   # daily metric, only available when the trading is in "done" state

    # Backtest data is included in the state.
    # Actually, only the time index of this data is needed, at this moment.
    # I include the full data so that algorithms (e.g., VWAP) that relies on the raw data can be implemented.

    backtest_data: IntradayBacktestData     # backtest data. interpreter should be careful not to leak feature

    # All possible trading ticks in all day, NOT sliced by order (defined in data). e.g., [9:30, 9:31, ..., 14:59]
    ticks_index: pd.DatetimeIndex


class SingleAssetOrderExecution(Simulator[Order, SAOEState, float]):
    """Single-asset order execution (SAOE) simulator.

    Parameters
    ----------
    initial
        The seed to start an SAOE simulator is an order.
    time_per_step
        Elapsed time per step.
    data_dir
        Path to load backtest data
    vol_threshold
        Maximum execution volume (divided by market execution volume).
    """

    history_exec: pd.DataFrame
    """All execution history at every possible time ticks. See :class:`SAOEMetrics` for fields."""

    history_steps: pd.DataFrame
    """Positions at each step. The position before first step is also recorded."""

    metrics: SAOEMetrics | None
    """Metrics. Only available when done."""

    def __init__(self, order: Order, data_dir: Path,
                 time_per_step: str = '30min',
                 deal_price_type: DealPriceType = 'close',
                 vol_threshold: float | None = None) -> None:
        self.order = order
        self.time_per_step = pd.Timedelta(time_per_step)
        self.deal_price_type = deal_price_type
        self.vol_threshold = vol_threshold
        self.data_dir = data_dir
        self.backtest_data = get_intraday_backtest_data(
            self.data_dir,
            order.stock_id,
            pd.Timestamp(order.start_time.date()),
            self.deal_price_type,
            order.direction
        )
        self.cur_time = self.backtest_data.get_time_index()[0]

        self.position = order.amount

        metric_keys = list(SAOEMetrics.__annotations__.keys())
        self.history_exec = pd.DataFrame(columns=metric_keys, index='datetime')
        self.history_steps = pd.DataFrame(columns=metric_keys, index='datetime')
        self.metrics = None

        self.market_price: np.ndarray | None = None
        self.market_vol: np.ndarray | None = None
        self.market_vol_limit: np.ndarray | None = None

    def step(self, amount: float) -> None:
        """Execute one step or SAOE.

        Parameters
        ----------
        amount
            The amount you wish to deal. The simulator doesn't guarantee all the amount to be successfully dealt.
        """

        exec_vol = self._split_exec_vol(amount)

        ticks_position = self.position - np.cumsum(exec_vol)

        self.position -= exec_vol.sum()
        if self.position < -EPS or (exec_vol < -EPS).any():
            raise ValueError(f'Execution volume is invalid: {exec_vol} (position = {self.position})')

        self.history_exec.append(pd.DataFrame(dict(
            datetime=self.backtest_data.get_time_index().slice_indexer(self.cur_time, self._next_time() - ONE_SEC),
            market_volume=self.market_vol,
            market_price=self.market_price,
            amount=exec_vol,
            inner_amount=exec_vol,
            deal_amount=exec_vol,
            trade_price=self.market_price,
            trade_value=self.market_price * exec_vol,
            position=ticks_position,
            ffr=exec_vol / self.order.amount,
            pa=price_advantage(self.market_price, self.market_price, self.order.direction)
        ), index='datetime')])

        self.history_steps.append(self._metrics_collect(self.cur_time, self.market_vol, self.market_price, amount, exec_vol))

        if self.done():
            self.metrics = self._metrics_collect(
                self.backtest_data.get_time_index()[0],  # start time
                self.history_exec['market_volume'],
                self.history_exec['market_price'],
                self.history_steps['amount'].sum(),
                self.history_exec['inner_amount'],
            )

        self.cur_time = self._next_time()

    def get_state(self) -> SAOEState:
        ticks_index = self.backtest_data.get_time_index()
        return SAOEState(
            order=self.order,
            cur_time=self.cur_time,
            position=self.position,
            history_exec=self.history_exec,
            history_steps=self.history_steps,
            backtest_data=self.backtest_data,
            ticks_index=ticks_index
        )

    def done(self) -> bool:
        return self.position < EPS or self.cur_time >= self.order.end_time

    def _next_time(self) -> pd.Timestamp:
        """The "current time" (``cur_time``) for next step."""
        return min(self.order.end_time, self.cur_time + self.time_per_step)

    def _cur_duration(self) -> pd.Timedelta:
        """The "duration" of this step (step that is about to happen)."""
        return self._next_time() - self.cur_time

    def _split_exec_vol(self, exec_vol_sum: float) -> np.ndarray:
        """
        Split the volume in each step into minutes, considering possible constraints.
        This follows TWAP strategy.
        """
        next_time = self._next_time()

        # get the backtest data for next interval
        self.market_vol = self.backtest_data.get_volume().loc[self.cur_time:next_time - ONE_SEC].to_numpy()
        self.market_price = self.backtest_data.get_deal_price(self.order.direction) \
            .loc[self.cur_time:next_time - ONE_SEC].to_numpy()

        # split the volume equally into each minute
        exec_vol = np.repeat(exec_vol_sum / len(self.market_price), len(self.market_price))

        # apply the volume threshold
        market_vol_limit = self.vol_threshold * self.market_vol if self.vol_threshold is not None else np.inf
        exec_vol = np.minimum(exec_vol, market_vol_limit)

        # Complete all the order amount at the last moment.
        if next_time == self.order.end_time:
            exec_vol[-1] += self.position - exec_vol.sum()
            exec_vol = np.minimum(exec_vol, market_vol_limit)

        return exec_vol

    def _metrics_collect(self, datetime: pd.Timestamp,
                         market_vol: np.ndarray,
                         market_price: np.ndarray,
                         amount: float,
                         exec_vol: np.ndarray) -> SAOEMetrics:
        assert len(market_vol) == len(market_price) == len(exec_vol)

        exec_avg_price = np.average(market_price, weights=exec_vol),  # could be nan

        return SAOEMetrics(
            datetime=datetime,
            market_volume=market_vol.sum(),
            market_price=market_price.mean(),
            amount=amount,
            inner_amount=exec_vol.sum(),
            deal_amount=exec_vol.sum(),  # in this simulator, there's no other restrictions
            trade_price=exec_avg_price,
            trade_value=np.sum(market_price * exec_vol),
            position=self.position,
            ffr=market_vol.sum() / self.order.amount,
            pa=price_advantage(exec_avg_price, self.backtest_data, self.order.direction)
        )


def price_advantage(exec_price: float, baseline_price: float, direction: OrderDir) -> float:
    if baseline_price == 0:  # something is wrong with data. Should be nan here
        return 0.
    if np.isnan(exec_price):
        return 0.
    if direction == OrderDir.BUY:
        return (1 - exec_price / baseline_price) * 10000
    elif direction == OrderDir.SELL:
        return (exec_price / baseline_price - 1) * 10000
    raise ValueError(f'Unexpected order direction: {direction}')