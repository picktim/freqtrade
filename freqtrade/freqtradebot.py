"""
Freqtrade is the main module of this bot. It contains the class Freqtrade()
"""
import asyncio
import logging
import traceback
from copy import deepcopy
from datetime import datetime, time, timedelta, timezone
from math import isclose
from threading import Lock
from time import sleep
from typing import Any, Dict, List, Optional, Tuple

from schedule import Scheduler

from freqtrade import constants
from freqtrade.configuration import validate_config_consistency
from freqtrade.constants import BuySell, Config, EntryExecuteMode, ExchangeConfig, LongShort
from freqtrade.data.converter import order_book_to_dataframe
from freqtrade.data.dataprovider import DataProvider
from freqtrade.edge import Edge
from freqtrade.enums import (ExitCheckTuple, ExitType, RPCMessageType, SignalDirection, State,
                             TradingMode)
from freqtrade.exceptions import (DependencyException, ExchangeError, InsufficientFundsError,
                                  InvalidOrderException, PricingError)
from freqtrade.exchange import (ROUND_DOWN, ROUND_UP, remove_exchange_credentials,
                                timeframe_to_minutes, timeframe_to_next_date, timeframe_to_seconds)
from freqtrade.misc import safe_value_fallback, safe_value_fallback2
from freqtrade.mixins import LoggingMixin
from freqtrade.persistence import Order, PairLocks, Trade, init_db
from freqtrade.persistence.key_value_store import set_startup_time
from freqtrade.plugins.pairlistmanager import PairListManager
from freqtrade.plugins.protectionmanager import ProtectionManager
from freqtrade.resolvers import ExchangeResolver, StrategyResolver
from freqtrade.rpc import RPCManager
from freqtrade.rpc.external_message_consumer import ExternalMessageConsumer
from freqtrade.rpc.rpc_types import (ProfitLossStr, RPCCancelMsg, RPCEntryMsg, RPCExitCancelMsg,
                                     RPCExitMsg, RPCProtectionMsg)
from freqtrade.strategy.interface import IStrategy
from freqtrade.strategy.strategy_wrapper import strategy_safe_wrapper
from freqtrade.util import FtPrecise
from freqtrade.util.migrations import migrate_binance_futures_names
from freqtrade.wallets import Wallets
from freqtrade.util.disk_cache import EntityCache

logger = logging.getLogger(__name__)


class FreqtradeBot(LoggingMixin):
    """
    Freqtrade is the main class of the bot.
    This is from here the bot start its logic.
    """

    def __init__(self, config: Config, loop: asyncio.AbstractEventLoop = None) -> None:
        """
        Init all variables and objects the bot needs to work
        :param config: configuration dict, you can use Configuration.get_config()
        to get the config dict.
        """
        self.loop = loop
        self.active_pair_whitelist: List[str] = []

        # Init bot state
        self.state = State.STOPPED

        #init tasks list 

        self.task_list : Dict[str,asyncio.Task] = {}

        # Init objects
        self.config = config
        exchange_config: ExchangeConfig = deepcopy(config['exchange'])
        # Remove credentials from original exchange config to avoid accidental credentail exposure
        remove_exchange_credentials(config['exchange'], True)

        self.strategy: IStrategy = StrategyResolver.load_strategy(self.config)

        # Check config consistency here since strategies can set certain options
        validate_config_consistency(config)

        self.exchange = ExchangeResolver.load_exchange(
            self.config, exchange_config=exchange_config, load_leverage_tiers=True)

        self.exchange.loop = loop

        init_db(self.config['db_url'])

        self.wallets = Wallets(self.config, self.exchange)

        PairLocks.timeframe = self.config['timeframe']

        self.pairlists = PairListManager(self.exchange, self.config)
        self.trading_mode: TradingMode = self.config.get('trading_mode', TradingMode.SPOT)
        self.last_process: Optional[datetime] = None

        # RPC runs in separate threads, can start handling external commands just after
        # initialization, even before Freqtradebot has a chance to start its throttling,
        # so anything in the Freqtradebot instance should be ready (initialized), including
        # the initial state of the bot.
        # Keep this at the end of this initialization method.
        self.rpc: RPCManager = RPCManager(self)

        self.dataprovider = DataProvider(self.config, self.exchange, rpc=self.rpc)
        self.pairlists = PairListManager(self.exchange, self.config, self.dataprovider)

        self.dataprovider.add_pairlisthandler(self.pairlists)

        # Attach Dataprovider to strategy instance
        self.strategy.dp = self.dataprovider
        # Attach Wallets to strategy instance
        self.strategy.wallets = self.wallets

        # Initializing Edge only if enabled
        self.edge = Edge(self.config, self.exchange, self.strategy) if \
            self.config.get('edge', {}).get('enabled', False) else None

        # Init ExternalMessageConsumer if enabled
        self.emc = ExternalMessageConsumer(self.config, self.dataprovider) if \
            self.config.get('external_message_consumer', {}).get('enabled', False) else None

        # self.active_pair_whitelist = self._refresh_active_whitelist()

        # Set initial bot state from config
        initial_state = self.config.get('initial_state')
        self.state = State[initial_state.upper()] if initial_state else State.STOPPED
        
        self.ta_cached = EntityCache.instance(self.config)

        # Protect exit-logic from forcesell and vice versa
        self._exit_lock = Lock()
        LoggingMixin.__init__(self, logger, timeframe_to_seconds(self.strategy.timeframe))

        self._schedule = Scheduler()

        if self.trading_mode == TradingMode.FUTURES:

            # def update():
            #     self.update_funding_fees()
            #     asyncio.get_event_loop().call_soon_threadsafe( self.wallets.update())

            # TODO: This would be more efficient if scheduled in utc time, and performed at each
            # TODO: funding interval, specified by funding_fee_times on the exchange classes
            for time_slot in range(0, 24):
                for minutes in [1,31]:
                    t = str(time(time_slot, minutes, 2))
                    self._schedule.every().day.at(t).do(self.update)
        self.strategy.ft_bot_start()
        # Initialize protections AFTER bot start - otherwise parameters are not loaded.
        self.protections = ProtectionManager(self.config, self.strategy.protections)


    def update(self):
        asyncio.get_event_loop().create_task( self.update_funding_fees())
        asyncio.get_event_loop().create_task( self.wallets.update())



    def notify_status(self, msg: str, msg_type=RPCMessageType.STATUS) -> None:
        """
        Public method for users of this class (worker, etc.) to send notifications
        via RPC about changes in the bot status.
        """
        self.rpc.send_msg({
            'type': msg_type,
            'status': msg
        })

    async def cleanup(self) -> None:
        """
        Cleanup pending resources on an already stopped bot
        :return: None
        """
        logger.info('Cleaning up modules ...')
        try:
            # Wrap db activities in shutdown to avoid problems if database is gone,
            # and raises further exceptions.
            if self.config['cancel_open_orders_on_exit']:
                await self.cancel_all_open_orders()

            self.check_for_open_trades()
        except Exception as e:
            logger.warning(f'Exception during cleanup: {e.__class__.__name__} {e}')

        finally:
            self.strategy.ft_bot_cleanup()

        self.rpc.cleanup()
        if self.emc:
            self.emc.shutdown()
        self.exchange.close()
        try:
            Trade.commit()
        except Exception:
            # Exeptions here will be happening if the db disappeared.
            # At which point we can no longer commit anyway.
            pass

    async def startup(self) -> None:
        """
        Called on startup and after reloading the bot - triggers notifications and
        performs startup tasks
        """
        migrate_binance_futures_names(self.config)
        set_startup_time()

        self.rpc.startup_messages(self.config, self.pairlists, self.protections)
        # Update older trades with precision and precision mode
        self.startup_backpopulate_precision()
        if not self.edge:
            # Adjust stoploss if it was changed
            Trade.stoploss_reinitialization(self.strategy.stoploss)

        # Only update open orders on startup
        # This will update the database after the initial migration
        await self.startup_update_open_orders()
        await self.update_funding_fees()

    async def process(self) -> None:
        """
        Queries the persistence layer for open trades and handles them,
        otherwise a new trade is created.
        :return: True if one or more trades has been created or closed, False otherwise
        """

        # Check whether markets have to be reloaded and reload them when it's needed
        await self.exchange.reload_markets()

        self.update_trades_without_assigned_fees()

        # Query trades from persistence layer
        trades: List[Trade] = Trade.get_open_trades()

        self.active_pair_whitelist = await self._refresh_active_whitelist(trades)

        # Refreshing candles
        await self.dataprovider.refresh(self.pairlists.create_pair_list(self.active_pair_whitelist),
                                  self.strategy.gather_informative_pairs())

        strategy_safe_wrapper(self.strategy.bot_loop_start, supress_error=True)(
            current_time=datetime.now(timezone.utc))
        print('beginning task created..')

        for pair in self.active_pair_whitelist :

            if self.task_list.get(pair) is None:
                task = asyncio .get_event_loop().create_task(self.strategy.analyze_pair(pair), name= pair) 
                task.add_done_callback(self.task_analyze_pair_complete_handle)
                self.task_list[pair] = task

        with self._exit_lock:
            # Check for exchange cancelations, timeouts and user requested replace
            await self.manage_open_orders()

        # Protect from collisions with force_exit.
        # Without this, freqtrade may try to recreate stoploss_on_exchange orders
        # while exiting is in process, since telegram messages arrive in an different thread.
        # with self._exit_lock:
        #     trades = Trade.get_open_trades()
        #     # First process current opened trades (positions)
        #     await self.exit_positions(trades)

        # Check if we need to adjust our current positions before attempting to buy new trades.
        # if self.strategy.position_adjustment_enable:
        #     with self._exit_lock:
        #         await self.process_open_trade_positions()

        # Then looking for buy opportunities
        # if self.get_free_open_trades():
        #     await self.enter_positions()
        if self.trading_mode == TradingMode.FUTURES:
            self._schedule.run_pending()
        # Trade.commit()
        self.rpc.process_msg_queue(self.dataprovider._msg_queue)
        self.last_process = datetime.now(timezone.utc)



    async def process_stopped(self) -> None:
        """
        Close all orders that were left open
        """
        if self.config['cancel_open_orders_on_exit']:
            await self.cancel_all_open_orders()


            

    def task_analyze_pair_complete_handle(self, task:asyncio.Task) :
        
        # pair :str = task.get_name()
        pair = task.get_name()
        print('task analyze complete name : {}'.format(pair))
        if not task.cancelled():
            result = task.result()
            del self.task_list[pair] 
            if result is not None and self.get_free_open_trades():
                print(result)
                new_task = asyncio.get_event_loop().create_task(self.enter_position(pair), name = pair)
                task.add_done_callback(self.enter_position_complete_handle)
                self.task_list[pair] = new_task

    def enter_position_complete_handle (self, task: asyncio.Task) :

        print('task enter position complete name : {}'.format(task.get_name()))
        
        del self.task_list[task.get_name()] 

    def task_manage_open_order_complete_handle( self, task: asyncio.Task) :
        print('task manage order complete name : {}'.format(task.get_name()))
        del self.task_list[task.get_name()]
        trades =  Trade.get_open_trade(pair = task.get_name())
        if trades is not None and len(trades) > 0:
            trade = trades[0]
            task = asyncio.get_event_loop().create_task(self.exit_position(trade), name = trade.pair)
            task.add_done_callback(self.task_exit_position_complete_handle)
            self.task_list[trade.pair] = task

    def task_exit_position_complete_handle(self, task: asyncio.Task ) :
        print('task exit complete name : {}'.format(task.get_name()))
        Trade.commit()
        del self.task_list[task.get_name()] 
        if  self.strategy.position_adjustment_enable == False :
            return
        
        trades =  Trade.get_open_trade(pair = task.get_name())
        if trades is not None and len(trades) > 0 :
            trade = trades[0]
            task = asyncio.get_event_loop().create_task(self.process_open_trade_position(trade), name = trade.pair)
            task.add_done_callback(self.task_process_open_trade_complete_handle)
            self.task_list[trade.pair] = task

    def task_process_open_trade_complete_handle(self, task:asyncio.Task) :
        print('task process_open_trade complete name : {}'.format(task.get_name()))
        del self.task_list[task.get_name()] 


    async def manage_open_orders(self) -> None:
        """
        Management of open orders on exchange. Unfilled orders might be cancelled if timeout
        was met or replaced if there's a new candle and user has requested it.
        Timeout setting takes priority over limit order adjustment request.
        :return: None
        """
        for trade in Trade.get_open_trades():
            if self.task_list.get(trade.pair) is None:
                task = asyncio.get_event_loop().create_task(self.execute_manage_open_order(trade), name = trade.pair)
                task.add_done_callback(self.task_manage_open_order_complete_handle)
                self.task_list[trade.pair] = task
            

    def check_for_open_trades(self):
        """
        Notify the user when the bot is stopped (not reloaded)
        and there are still open trades active.
        """
        open_trades = Trade.get_open_trades()

        if len(open_trades) != 0 and self.state != State.RELOAD_CONFIG:
            msg = {
                'type': RPCMessageType.WARNING,
                'status':
                    f"{len(open_trades)} open trades active.\n\n"
                    f"Handle these trades manually on {self.exchange.name}, "
                    f"or '/start' the bot again and use '/stopentry' "
                    f"to handle open trades gracefully. \n"
                    f"{'Note: Trades are simulated (dry run).' if self.config['dry_run'] else ''}",
            }
            self.rpc.send_msg(msg)

    async def _refresh_active_whitelist(self, trades: List[Trade] = []) -> List[str]:
        """
        Refresh active whitelist from pairlist or edge and extend it with
        pairs that have open trades.
        """
        # Refresh whitelist
        _prev_whitelist = self.pairlists.whitelist
        await self.pairlists.refresh_pairlist()
        _whitelist = self.pairlists.whitelist

        # Calculating Edge positioning
        if self.edge:
            self.edge.calculate(_whitelist)
            _whitelist = self.edge.adjust(_whitelist)

        if trades:
            # Extend active-pair whitelist with pairs of open trades
            # It ensures that candle (OHLCV) data are downloaded for open trades as well
            # _whitelist.extend([trade.pair for trade in trades if trade.pair not in _whitelist])
            for trade in trades :
                if trade.pair in _whitelist :
                    _whitelist.remove(trade.pair)
            # _whitelist.remove([trade.pair for trade in trades if trade.pair  in _whitelist])
        # Called last to include the included pairs
        if _prev_whitelist != _whitelist:
            self.rpc.send_msg({'type': RPCMessageType.WHITELIST, 'data': _whitelist})

        return _whitelist

    def get_free_open_trades(self) -> int:
        """
        Return the number of free open trades slots or 0 if
        max number of open trades reached
        """
        open_trades = Trade.get_open_trade_count()
        return max(0, self.config['max_open_trades'] - open_trades)

    async def update_funding_fees(self) -> None:
        if self.trading_mode == TradingMode.FUTURES:
            trades: List[Trade] = Trade.get_open_trades()
            for trade in trades:
                trade.set_funding_fees(
                    await self.exchange.get_funding_fees(
                        pair=trade.pair,
                        amount=trade.amount,
                        is_short=trade.is_short,
                        open_date=trade.date_last_filled_utc)
                )

    def startup_backpopulate_precision(self) -> None:

        trades = Trade.get_trades([Trade.contract_size.is_(None)])
        for trade in trades:
            if trade.exchange != self.exchange.id:
                continue
            trade.precision_mode = self.exchange.precisionMode
            trade.amount_precision = self.exchange.get_precision_amount(trade.pair)
            trade.price_precision = self.exchange.get_precision_price(trade.pair)
            trade.contract_size = self.exchange.get_contract_size(trade.pair)
        Trade.commit()

    async def startup_update_open_orders(self):
        """
        Updates open orders based on order list kept in the database.
        Mainly updates the state of orders - but may also close trades
        """
        if self.config['dry_run'] or self.config['exchange'].get('skip_open_order_update', False):
            # Updating open orders in dry-run does not make sense and will fail.
            return

        orders = Order.get_open_orders()
        logger.info(f"Updating {len(orders)} open orders.")
        for order in orders:
            try:
                fo = await self.exchange.fetch_order_or_stoploss_order(order.order_id, order.ft_pair,
                                                                 order.ft_order_side == 'stoploss')
                if not order.trade:
                    # This should not happen, but it does if trades were deleted manually.
                    # This can only incur on sqlite, which doesn't enforce foreign constraints.
                    logger.warning(
                        f"Order {order.order_id} has no trade attached. "
                        "This may suggest a database corruption. "
                        f"The expected trade ID is {order.ft_trade_id}. Ignoring this order."
                    )
                    continue
                await self.update_trade_state(order.trade, order.order_id, fo,
                                        stoploss_order=(order.ft_order_side == 'stoploss'))

            except InvalidOrderException as e:
                logger.warning(f"Error updating Order {order.order_id} due to {e}.")
                if order.order_date_utc - timedelta(days=5) < datetime.now(timezone.utc):
                    logger.warning(
                        "Order is older than 5 days. Assuming order was fully cancelled.")
                    fo = order.to_ccxt_object()
                    fo['status'] = 'canceled'
                    self.handle_cancel_order(
                        fo, order, order.trade, constants.CANCEL_REASON['TIMEOUT']
                    )

            except ExchangeError as e:

                logger.warning(f"Error updating Order {order.order_id} due to {e}")

    async def update_trades_without_assigned_fees(self) -> None:
        """
        Update closed trades without close fees assigned.
        Only acts when Orders are in the database, otherwise the last order-id is unknown.
        """
        if self.config['dry_run']:
            # Updating open orders in dry-run does not make sense and will fail.
            return

        trades: List[Trade] = Trade.get_closed_trades_without_assigned_fees()
        for trade in trades:
            if not trade.is_open and not trade.fee_updated(trade.exit_side):
                # Get sell fee
                order = trade.select_order(trade.exit_side, False, only_filled=True)
                if not order:
                    order = trade.select_order('stoploss', False)
                if order:
                    logger.info(
                        f"Updating {trade.exit_side}-fee on trade {trade}"
                        f"for order {order.order_id}."
                    )
                    await self.update_trade_state(trade, order.order_id,
                                            stoploss_order=order.ft_order_side == 'stoploss',
                                            send_msg=False)

        trades = Trade.get_open_trades_without_assigned_fees()
        for trade in trades:
            with self._exit_lock:
                if trade.is_open and not trade.fee_updated(trade.entry_side):
                    order = trade.select_order(trade.entry_side, False, only_filled=True)
                    open_order = trade.select_order(trade.entry_side, True)
                    if order and open_order is None:
                        logger.info(
                            f"Updating {trade.entry_side}-fee on trade {trade}"
                            f"for order {order.order_id}."
                        )
                        self.update_trade_state(trade, order.order_id, send_msg=False)

    async def handle_insufficient_funds(self, trade: Trade):
        """
        Try refinding a lost trade.
        Only used when InsufficientFunds appears on exit orders (stoploss or long sell/short buy).
        Tries to walk the stored orders and updates the trade state if necessary.
        """
        logger.info(f"Trying to refind lost order for {trade}")
        for order in trade.orders:
            logger.info(f"Trying to refind {order}")
            fo = None
            if not order.ft_is_open:
                logger.debug(f"Order {order} is no longer open.")
                continue
            try:
                fo = await self.exchange.fetch_order_or_stoploss_order(order.order_id, order.ft_pair,
                                                                 order.ft_order_side == 'stoploss')
                if order.ft_order_side == 'stoploss':
                    if fo and fo['status'] == 'open':
                        # Assume this as the open stoploss order
                        trade.stoploss_order_id = order.order_id
                if fo:
                    logger.info(f"Found {order} for trade {trade}.")
                    await self.update_trade_state(trade, order.order_id, fo,
                                            stoploss_order=order.ft_order_side == 'stoploss')

            except ExchangeError:
                logger.warning(f"Error updating {order.order_id}.")

    async def handle_onexchange_order(self, trade: Trade):
        """
        Try refinding a order that is not in the database.
        Only used balance disappeared, which would make exiting impossible.
        """
        try:
            orders = await self.exchange.fetch_orders(
                trade.pair, trade.open_date_utc - timedelta(seconds=10))
            prev_exit_reason = trade.exit_reason
            prev_trade_state = trade.is_open
            for order in orders:
                trade_order = [o for o in trade.orders if o.order_id == order['id']]

                if trade_order:
                    # We knew this order, but didn't have it updated properly
                    order_obj = trade_order[0]
                else:
                    logger.info(f"Found previously unknown order {order['id']} for {trade.pair}.")

                    order_obj = Order.parse_from_ccxt_object(order, trade.pair, order['side'])
                    order_obj.order_filled_date = datetime.fromtimestamp(
                        safe_value_fallback(order, 'lastTradeTimestamp', 'timestamp') // 1000,
                        tz=timezone.utc)
                    trade.orders.append(order_obj)
                    Trade.commit()
                    trade.exit_reason = ExitType.SOLD_ON_EXCHANGE.value

                await self.update_trade_state(trade, order['id'], order, send_msg=False)

                logger.info(f"handled order {order['id']}")

            # Refresh trade from database
            Trade.session.refresh(trade)
            if not trade.is_open:
                # Trade was just closed
                trade.close_date = trade.date_last_filled_utc
                await self.order_close_notify(trade, order_obj,
                                        order_obj.ft_order_side == 'stoploss',
                                        send_msg=prev_trade_state != trade.is_open)
            else:
                trade.exit_reason = prev_exit_reason
            Trade.commit()

        except ExchangeError:
            logger.warning("Error finding onexchange order.")
        except Exception:
            # catching https://github.com/freqtrade/freqtrade/issues/9025
            logger.warning("Error finding onexchange order", exc_info=True)
#
# BUY / enter positions / open trades logic and methods
#
    async def enter_position(self, pair:str) -> int:
        if PairLocks.is_global_lock(side='*'):
            # This only checks for total locks (both sides).
            # per-side locks will be evaluated by `is_pair_locked` within create_trade,
            # once the direction for the trade is clear.
            lock = PairLocks.get_pair_longest_lock('*')
            if lock:
                self.log_once(f"Global pairlock active until "
                              f"{lock.lock_end_time.strftime(constants.DATETIME_PRINT_FORMAT)}. "
                              f"Not creating new trades, reason: {lock.reason}.", logger.info)
            else:
                self.log_once("Global pairlock active. Not creating new trades.", logger.info)
            return
        
            
        try:
            with self._exit_lock:
                await self.create_trade(pair)
        except DependencyException as exception:
            logger.warning('Unable to create trade for %s: %s', pair, exception)

        
    async def enter_positions(self) -> int:
        """
        Tries to execute entry orders for new trades (positions)
        """
        trades_created = 0

        whitelist = deepcopy(self.active_pair_whitelist)
        if not whitelist:
            self.log_once("Active pair whitelist is empty.", logger.info)
            return trades_created
        # Remove pairs for currently opened trades from the whitelist
        for trade in Trade.get_open_trades():
            if trade.pair in whitelist:
                whitelist.remove(trade.pair)
                logger.debug('Ignoring %s in pair whitelist', trade.pair)

        if not whitelist:
            self.log_once("No currency pair in active pair whitelist, "
                          "but checking to exit open trades.", logger.info)
            return trades_created
        if PairLocks.is_global_lock(side='*'):
            # This only checks for total locks (both sides).
            # per-side locks will be evaluated by `is_pair_locked` within create_trade,
            # once the direction for the trade is clear.
            lock = PairLocks.get_pair_longest_lock('*')
            if lock:
                self.log_once(f"Global pairlock active until "
                              f"{lock.lock_end_time.strftime(constants.DATETIME_PRINT_FORMAT)}. "
                              f"Not creating new trades, reason: {lock.reason}.", logger.info)
            else:
                self.log_once("Global pairlock active. Not creating new trades.", logger.info)
            return trades_created
        # Create entity and execute trade for each pair from whitelist
        for pair in whitelist:
            try:
                with self._exit_lock:
                    trades_created += await self.create_trade(pair)
            except DependencyException as exception:
                logger.warning('Unable to create trade for %s: %s', pair, exception)
                # traceback.print_exc() 

        if not trades_created:
            logger.debug("Found no enter signals for whitelisted currencies. Trying again...")

        return trades_created

    async def create_trade(self, pair: str) -> bool:
        """
        Check the implemented trading strategy for buy signals.

        If the pair triggers the buy signal a new trade record gets created
        and the buy-order opening the trade gets issued towards the exchange.

        :return: True if a trade has been created.
        """
        logger.debug(f"create_trade for pair {pair}")

        analyzed_df, _ = self.dataprovider.get_analyzed_dataframe(pair, self.strategy.timeframe)
        nowtime = analyzed_df.iloc[-1]['date'] if len(analyzed_df) > 0 else None

        # get_free_open_trades is checked before create_trade is called
        # but it is still used here to prevent opening too many trades within one iteration
        if not self.get_free_open_trades():
            logger.debug(f"Can't open a new trade for {pair}: max number of trades is reached.")
            return False

        # running get_signal on historical data fetched
        (signal, enter_tag) = self.strategy.get_entry_signal(
            pair,
            self.strategy.timeframe,
            analyzed_df
        )

        if signal:
            if self.strategy.is_pair_locked(pair, candle_date=nowtime, side=signal):
                lock = PairLocks.get_pair_longest_lock(pair, nowtime, signal)
                if lock:
                    self.log_once(f"Pair {pair} {lock.side} is locked until "
                                  f"{lock.lock_end_time.strftime(constants.DATETIME_PRINT_FORMAT)} "
                                  f"due to {lock.reason}.",
                                  logger.info)
                else:
                    self.log_once(f"Pair {pair} is currently locked.", logger.info)
                return False
            stake_amount = await self.wallets.get_trade_stake_amount(
                pair, self.config['max_open_trades'], self.edge)

            bid_check_dom = self.config.get('entry_pricing', {}).get('check_depth_of_market', {})
            if ((bid_check_dom.get('enabled', False)) and
                    (bid_check_dom.get('bids_to_ask_delta', 0) > 0)):
                if await self._check_depth_of_market(pair, bid_check_dom, side=signal):
                    return await self.execute_entry(
                        pair,
                        stake_amount,
                        enter_tag=enter_tag,
                        is_short=(signal == SignalDirection.SHORT)
                    )
                else:
                    return False

            return await self.execute_entry(
                pair,
                stake_amount,
                enter_tag=enter_tag,
                is_short=(signal == SignalDirection.SHORT)
            )
        else:
            return False

#
# BUY / increase positions / DCA logic and methods
#
    async def process_open_trade_position (self, trade: Trade) :
           if not trade.has_open_orders:
                await self.wallets.update(False)
                try:
                    await self.check_and_call_adjust_trade_position(trade)
                except DependencyException as exception:
                    logger.warning(
                        f"Unable to adjust position of trade for {trade.pair}: {exception}")
    
    async def process_open_trade_positions(self):
        """
        Tries to execute additional buy or sell orders for open trades (positions)
        """
        # Walk through each pair and check if it needs changes
        for trade in Trade.get_open_trades():
            # If there is any open orders, wait for them to finish.
            # TODO Remove to allow mul open orders
            if not trade.has_open_orders:
                # Do a wallets update (will be ratelimited to once per hour)
                await self.wallets.update(False)
                try:
                    await self.check_and_call_adjust_trade_position(trade)
                except DependencyException as exception:
                    logger.warning(
                        f"Unable to adjust position of trade for {trade.pair}: {exception}")

    async def check_and_call_adjust_trade_position(self, trade: Trade):
        """
        Check the implemented trading strategy for adjustment command.
        If the strategy triggers the adjustment, a new order gets issued.
        Once that completes, the existing trade is modified to match new data.
        """
        current_entry_rate, current_exit_rate = await self.exchange.get_rates(
            trade.pair, True, trade.is_short)

        current_entry_profit = trade.calc_profit_ratio(current_entry_rate)
        current_exit_profit = trade.calc_profit_ratio(current_exit_rate)

        min_entry_stake = self.exchange.get_min_pair_stake_amount(trade.pair,
                                                                  current_entry_rate,
                                                                  0.0)
        min_exit_stake = self.exchange.get_min_pair_stake_amount(trade.pair,
                                                                 current_exit_rate,
                                                                 self.strategy.stoploss)
        max_entry_stake = self.exchange.get_max_pair_stake_amount(trade.pair, current_entry_rate)
        stake_available = self.wallets.get_available_stake_amount()
        logger.debug(f"Calling adjust_trade_position for pair {trade.pair}")
        stake_amount = strategy_safe_wrapper(self.strategy.adjust_trade_position,
                                             default_retval=None, supress_error=True)(
            trade=trade,
            current_time=datetime.now(timezone.utc), current_rate=current_entry_rate,
            current_profit=current_entry_profit, min_stake=min_entry_stake,
            max_stake=min(max_entry_stake, stake_available),
            current_entry_rate=current_entry_rate, current_exit_rate=current_exit_rate,
            current_entry_profit=current_entry_profit, current_exit_profit=current_exit_profit
        )

        if stake_amount is not None and stake_amount > 0.0:
            # We should increase our position
            if self.strategy.max_entry_position_adjustment > -1:
                count_of_entries = trade.nr_of_successful_entries
                if count_of_entries > self.strategy.max_entry_position_adjustment:
                    logger.debug(f"Max adjustment entries for {trade.pair} has been reached.")
                    return
                else:
                    logger.debug("Max adjustment entries is set to unlimited.")
            await self.execute_entry(trade.pair, stake_amount, price=current_entry_rate,
                               trade=trade, is_short=trade.is_short, mode='pos_adjust')

        if stake_amount is not None and stake_amount < 0.0:
            # We should decrease our position
            amount = self.exchange.amount_to_contract_precision(
                trade.pair,
                abs(float(FtPrecise(stake_amount * trade.leverage) / FtPrecise(current_exit_rate))))

            if amount == 0.0:
                logger.info("Amount to exit is 0.0 due to exchange limits - not exiting.")
                return

            remaining = (trade.amount - amount) * current_exit_rate
            if min_exit_stake and remaining != 0 and remaining < min_exit_stake:
                logger.info(f"Remaining amount of {remaining} would be smaller "
                            f"than the minimum of {min_exit_stake}.")
                return

            await self.execute_trade_exit(trade, current_exit_rate, exit_check=ExitCheckTuple(
                exit_type=ExitType.PARTIAL_EXIT), sub_trade_amt=amount)

    async def _check_depth_of_market(self, pair: str, conf: Dict, side: SignalDirection) -> bool:
        """
        Checks depth of market before executing a buy
        """
        conf_bids_to_ask_delta = conf.get('bids_to_ask_delta', 0)
        logger.info(f"Checking depth of market for {pair} ...")
        order_book = await  self.exchange.fetch_l2_order_book(pair, 1000)
        order_book_data_frame = order_book_to_dataframe(order_book['bids'], order_book['asks'])
        order_book_bids = order_book_data_frame['b_size'].sum()
        order_book_asks = order_book_data_frame['a_size'].sum()

        entry_side = order_book_bids if side == SignalDirection.LONG else order_book_asks
        exit_side = order_book_asks if side == SignalDirection.LONG else order_book_bids
        bids_ask_delta = entry_side / exit_side

        bids = f"Bids: {order_book_bids}"
        asks = f"Asks: {order_book_asks}"
        delta = f"Delta: {bids_ask_delta}"

        logger.info(
            f"{bids}, {asks}, {delta}, Direction: {side.value}"
            f"Bid Price: {order_book['bids'][0][0]}, Ask Price: {order_book['asks'][0][0]}, "
            f"Immediate Bid Quantity: {order_book['bids'][0][1]}, "
            f"Immediate Ask Quantity: {order_book['asks'][0][1]}."
        )
        if bids_ask_delta >= conf_bids_to_ask_delta:
            logger.info(f"Bids to asks delta for {pair} DOES satisfy condition.")
            return True
        else:
            logger.info(f"Bids to asks delta for {pair} does not satisfy condition.")
            return False

    async def execute_entry(
        self,
        pair: str,
        stake_amount: float,
        price: Optional[float] = None,
        *,
        is_short: bool = False,
        ordertype: Optional[str] = None,
        enter_tag: Optional[str] = None,
        trade: Optional[Trade] = None,
        mode: EntryExecuteMode = 'initial',
        leverage_: Optional[float] = None,
    ) -> bool:
        """
        Executes a limit buy for the given pair
        :param pair: pair for which we want to create a LIMIT_BUY
        :param stake_amount: amount of stake-currency for the pair
        :return: True if a buy order is created, false if it fails.
        :raise: DependencyException or it's subclasses like ExchangeError.
        """
        time_in_force = self.strategy.order_time_in_force['entry']

        side: BuySell = 'sell' if is_short else 'buy'
        name = 'Short' if is_short else 'Long'
        trade_side: LongShort = 'short' if is_short else 'long'
        pos_adjust = trade is not None

        enter_limit_requested, stake_amount, leverage =  await self.get_valid_enter_price_and_stake(
            pair, price, stake_amount, trade_side, enter_tag, trade, mode, leverage_)

        if not stake_amount:
            return False

        msg = (f"Position adjust: about to create a new order for {pair} with stake_amount: "
               f"{stake_amount} for {trade}" if mode == 'pos_adjust'
               else
               (f"Replacing {side} order: about create a new order for {pair} with stake_amount: "
                f"{stake_amount} ..."
                if mode == 'replace' else
                f"{name} signal found: about create a new trade for {pair} with stake_amount: "
                f"{stake_amount} ..."
                ))
        logger.info(msg)
        amount = (stake_amount / enter_limit_requested) * leverage
        order_type = ordertype or self.strategy.order_types['entry']

        if mode == 'initial' and not strategy_safe_wrapper(
                self.strategy.confirm_trade_entry, default_retval=True)(
                pair=pair, order_type=order_type, amount=amount, rate=enter_limit_requested,
                time_in_force=time_in_force, current_time=datetime.now(timezone.utc),
                entry_tag=enter_tag, side=trade_side):
            logger.info(f"User denied entry for {pair}.")
            return False
        order = await self.exchange.create_order(
            pair=pair,
            ordertype=order_type,
            side=side,
            amount=amount,
            rate=enter_limit_requested,
            reduceOnly=False,
            time_in_force=time_in_force,
            leverage=leverage
        )
        order_obj = Order.parse_from_ccxt_object(order, pair, side, amount, enter_limit_requested)
        order_id = order['id']
        order_status = order.get('status')
        logger.info(f"Order {order_id} was created for {pair} and status is {order_status}.")

        # we assume the order is executed at the price requested
        enter_limit_filled_price = enter_limit_requested
        amount_requested = amount

        if order_status == 'expired' or order_status == 'rejected':

            # return false if the order is not filled
            if float(order['filled']) == 0:
                logger.warning(f'{name} {time_in_force} order with time in force {order_type} '
                               f'for {pair} is {order_status} by {self.exchange.name}.'
                               ' zero amount is fulfilled.')
                return False
            else:
                # the order is partially fulfilled
                # in case of IOC orders we can check immediately
                # if the order is fulfilled fully or partially
                logger.warning('%s %s order with time in force %s for %s is %s by %s.'
                               ' %s amount fulfilled out of %s (%s remaining which is canceled).',
                               name, time_in_force, order_type, pair, order_status,
                               self.exchange.name, order['filled'], order['amount'],
                               order['remaining']
                               )
                amount = safe_value_fallback(order, 'filled', 'amount', amount)
                enter_limit_filled_price = safe_value_fallback(
                    order, 'average', 'price', enter_limit_filled_price)

        # in case of FOK the order may be filled immediately and fully
        elif order_status == 'closed':
            amount = safe_value_fallback(order, 'filled', 'amount', amount)
            enter_limit_filled_price = safe_value_fallback(
                order, 'average', 'price', enter_limit_requested)

        # Fee is applied twice because we make a LIMIT_BUY and LIMIT_SELL
        fee = self.exchange.get_fee(symbol=pair, taker_or_maker='maker')
        base_currency = self.exchange.get_pair_base_currency(pair)
        open_date = datetime.now(timezone.utc)

        funding_fees = await self.exchange.get_funding_fees(
            pair=pair,
            amount=amount + trade.amount if trade else amount,
            is_short=is_short,
            open_date=trade.date_last_filled_utc if trade else open_date
        )

        # This is a new trade
        if trade is None:

            trade = Trade(
                pair=pair,
                base_currency=base_currency,
                stake_currency=self.config['stake_currency'],
                stake_amount=stake_amount,
                amount=amount,
                is_open=True,
                amount_requested=amount_requested,
                fee_open=fee,
                fee_close=fee,
                open_rate=enter_limit_filled_price,
                open_rate_requested=enter_limit_requested,
                open_date=open_date,
                exchange=self.exchange.id,
                strategy=self.strategy.get_strategy_name(),
                enter_tag=enter_tag,
                timeframe=timeframe_to_minutes(self.config['timeframe']),
                leverage=leverage,
                is_short=is_short,
                trading_mode=self.trading_mode,
                funding_fees=funding_fees,
                amount_precision=self.exchange.get_precision_amount(pair),
                price_precision=self.exchange.get_precision_price(pair),
                precision_mode=self.exchange.precisionMode,
                contract_size=self.exchange.get_contract_size(pair),
            )
            stoploss = self.strategy.stoploss if not self.edge else self.edge.get_stoploss(pair)
            trade.adjust_stop_loss(trade.open_rate, stoploss, initial=True)

        else:
            # This is additional buy, we reset fee_open_currency so timeout checking can work
            trade.is_open = True
            trade.fee_open_currency = None
            trade.open_rate_requested = enter_limit_requested
            trade.set_funding_fees(funding_fees)

        trade.orders.append(order_obj)
        trade.recalc_trade_from_orders()
        Trade.session.add(trade)
        Trade.commit()

        # Updating wallets
        await self.wallets.update()

        await self._notify_enter(trade, order_obj, order_type, sub_trade=pos_adjust)

        if pos_adjust:
            if order_status == 'closed':
                logger.info(f"DCA order closed, trade should be up to date: {trade}")
                trade = await self.cancel_stoploss_on_exchange(trade)
            else:
                logger.info(f"DCA order {order_status}, will wait for resolution: {trade}")

        # Update fees if order is non-opened
        if order_status in constants.NON_OPEN_EXCHANGE_STATES:
            await self.update_trade_state(trade, order_id, order)

        return True

    async def cancel_stoploss_on_exchange(self, trade: Trade) -> Trade:
        # First cancelling stoploss on exchange ...
        if trade.stoploss_order_id:
            try:
                logger.info(f"Cancelling stoploss on exchange for {trade}")
                co = await self.exchange.cancel_stoploss_order_with_result(
                    trade.stoploss_order_id, trade.pair, trade.amount)
                await self.update_trade_state(trade, trade.stoploss_order_id, co, stoploss_order=True)

                # Reset stoploss order id.
                trade.stoploss_order_id = None
            except InvalidOrderException:
                logger.exception(f"Could not cancel stoploss order {trade.stoploss_order_id} "
                                 f"for pair {trade.pair}")
        return trade

    async def get_valid_enter_price_and_stake(
        self, pair: str, price: Optional[float], stake_amount: float,
        trade_side: LongShort,
        entry_tag: Optional[str],
        trade: Optional[Trade],
        mode: EntryExecuteMode,
        leverage_: Optional[float],
    ) -> Tuple[float, float, float]:
        """
        Validate and eventually adjust (within limits) limit, amount and leverage
        :return: Tuple with (price, amount, leverage)
        """

        if price:
            enter_limit_requested = price
        else:
            # Calculate price
            enter_limit_requested = await self.exchange.get_rate(
                pair, side='entry', is_short=(trade_side == 'short'), refresh=True)
        if mode != 'replace':
            # Don't call custom_entry_price in order-adjust scenario
            custom_entry_price = strategy_safe_wrapper(self.strategy.custom_entry_price,
                                                       default_retval=enter_limit_requested)(
                pair=pair, trade=trade,
                current_time=datetime.now(timezone.utc),
                proposed_rate=enter_limit_requested, entry_tag=entry_tag,
                side=trade_side,
            )

            enter_limit_requested = self.get_valid_price(custom_entry_price, enter_limit_requested)

        if not enter_limit_requested:
            raise PricingError('Could not determine entry price.')

        if self.trading_mode != TradingMode.SPOT and trade is None:
            max_leverage = self.exchange.get_max_leverage(pair, stake_amount)
            if leverage_:
                leverage = leverage_
            else:
                leverage = strategy_safe_wrapper(self.strategy.leverage, default_retval=1.0)(
                    pair=pair,
                    current_time=datetime.now(timezone.utc),
                    current_rate=enter_limit_requested,
                    proposed_leverage=1.0,
                    max_leverage=max_leverage,
                    side=trade_side, entry_tag=entry_tag,
                )
            # Cap leverage between 1.0 and max_leverage.
            leverage = min(max(leverage, 1.0), max_leverage)
        else:
            # Changing leverage currently not possible
            leverage = trade.leverage if trade else 1.0

        # Min-stake-amount should actually include Leverage - this way our "minimal"
        # stake- amount might be higher than necessary.
        # We do however also need min-stake to determine leverage, therefore this is ignored as
        # edge-case for now.
        min_stake_amount = self.exchange.get_min_pair_stake_amount(
            pair, enter_limit_requested,
            self.strategy.stoploss if not mode != 'pos_adjust' else 0.0,
            leverage)
        max_stake_amount = self.exchange.get_max_pair_stake_amount(
            pair, enter_limit_requested, leverage)

        if not self.edge and trade is None:
            stake_available = self.wallets.get_available_stake_amount()
            stake_amount = strategy_safe_wrapper(self.strategy.custom_stake_amount,
                                                 default_retval=stake_amount)(
                pair=pair, current_time=datetime.now(timezone.utc),
                current_rate=enter_limit_requested, proposed_stake=stake_amount,
                min_stake=min_stake_amount, max_stake=min(max_stake_amount, stake_available),
                leverage=leverage, entry_tag=entry_tag, side=trade_side
            )

        stake_amount = self.wallets.validate_stake_amount(
            pair=pair,
            stake_amount=stake_amount,
            min_stake_amount=min_stake_amount,
            max_stake_amount=max_stake_amount,
            trade_amount=trade.stake_amount if trade else None,
        )

        return enter_limit_requested, stake_amount, leverage

    async def _notify_enter(self, trade: Trade, order: Order, order_type: str,
                      fill: bool = False, sub_trade: bool = False) -> None:
        """
        Sends rpc notification when a entry order occurred.
        """
        open_rate = order.safe_price

        if open_rate is None:
            open_rate = trade.open_rate

        current_rate = await self.exchange.get_rate(
            trade.pair, side='entry', is_short=trade.is_short, refresh=False)

        msg: RPCEntryMsg = {
            'trade_id': trade.id,
            'type': RPCMessageType.ENTRY_FILL if fill else RPCMessageType.ENTRY,
            'buy_tag': trade.enter_tag,
            'enter_tag': trade.enter_tag,
            'exchange': trade.exchange.capitalize(),
            'pair': trade.pair,
            'leverage': trade.leverage if trade.leverage else None,
            'direction': 'Short' if trade.is_short else 'Long',
            'limit': open_rate,  # Deprecated (?)
            'open_rate': open_rate,
            'order_type': order_type,
            'stake_amount': trade.stake_amount,
            'stake_currency': self.config['stake_currency'],
            'base_currency': self.exchange.get_pair_base_currency(trade.pair),
            'quote_currency': self.exchange.get_pair_quote_currency(trade.pair),
            'fiat_currency': self.config.get('fiat_display_currency', None),
            'amount': order.safe_amount_after_fee if fill else (order.amount or trade.amount),
            'open_date': trade.open_date_utc or datetime.now(timezone.utc),
            'current_rate': current_rate,
            'sub_trade': sub_trade,
        }

        # Send the message
        self.rpc.send_msg(msg)

    async def _notify_enter_cancel(self, trade: Trade, order_type: str, reason: str,
                             sub_trade: bool = False) -> None:
        """
        Sends rpc notification when a entry order cancel occurred.
        """
        current_rate = await self.exchange.get_rate(
            trade.pair, side='entry', is_short=trade.is_short, refresh=False)

        msg: RPCCancelMsg = {
            'trade_id': trade.id,
            'type': RPCMessageType.ENTRY_CANCEL,
            'buy_tag': trade.enter_tag,
            'enter_tag': trade.enter_tag,
            'exchange': trade.exchange.capitalize(),
            'pair': trade.pair,
            'leverage': trade.leverage,
            'direction': 'Short' if trade.is_short else 'Long',
            'limit': trade.open_rate,
            'order_type': order_type,
            'stake_amount': trade.stake_amount,
            'open_rate': trade.open_rate,
            'stake_currency': self.config['stake_currency'],
            'base_currency': self.exchange.get_pair_base_currency(trade.pair),
            'quote_currency': self.exchange.get_pair_quote_currency(trade.pair),
            'fiat_currency': self.config.get('fiat_display_currency', None),
            'amount': trade.amount,
            'open_date': trade.open_date,
            'current_rate': current_rate,
            'reason': reason,
            'sub_trade': sub_trade,
        }

        # Send the message
        self.rpc.send_msg(msg)

#
# SELL / exit positions / close trades logic and methods
#
        
    async def exit_position (self, trade:Trade )  :
         
        trade_closed : int = 0
        stoploss_on_exchange : bool = False
        if (
                not trade.has_open_orders
                and not trade.stoploss_order_id
                and not await self.wallets.check_exit_amount(trade)
            ):
                logger.warning(
                    f'Not enough {trade.safe_base_currency} in wallet to exit {trade}. '
                    'Trying to recover.')
                await self.handle_onexchange_order(trade)

                try:
                    try:
                        if (self.strategy.order_types.get('stoploss_on_exchange') and
                                self.handle_stoploss_on_exchange(trade)):
                            trade_closed += 1
                            Trade.commit()
                            stoploss_on_exchange = True

                    except InvalidOrderException as exception:
                        logger.warning(
                            f'Unable to handle stoploss on exchange for {trade.pair}: {exception}')
                    # Check if we can sell our current pair
                    if not stoploss_on_exchange:
                        if not trade.has_open_orders and trade.is_open and await self.handle_trade(trade):
                            trade_closed += 1

                except DependencyException as exception:
                    logger.warning(f'Unable to exit trade {trade.pair}: {exception}')

        if trade_closed > 0 :
            await self.wallets.update()


    async def exit_positions(self, trades: List[Trade]) -> int:
        """
        Tries to execute exit orders for open trades (positions)
        """
        trades_closed = 0
        for trade in trades:

            if (
                not trade.has_open_orders
                and not trade.stoploss_order_id
                and not await self.wallets.check_exit_amount(trade)
            ):
                logger.warning(
                    f'Not enough {trade.safe_base_currency} in wallet to exit {trade}. '
                    'Trying to recover.')
                await self.handle_onexchange_order(trade)

            try:
                try:
                    if (self.strategy.order_types.get('stoploss_on_exchange') and
                            self.handle_stoploss_on_exchange(trade)):
                        trades_closed += 1
                        Trade.commit()
                        continue

                except InvalidOrderException as exception:
                    logger.warning(
                        f'Unable to handle stoploss on exchange for {trade.pair}: {exception}')
                # Check if we can sell our current pair
                if not trade.has_open_orders and trade.is_open and await self.handle_trade(trade):
                    trades_closed += 1

            except DependencyException as exception:
                logger.warning(f'Unable to exit trade {trade.pair}: {exception}')

        # Updating wallets if any trade occurred
        if trades_closed:
            await self.wallets.update()

        return trades_closed

    async def handle_trade(self, trade: Trade) -> bool:
        """
        Exits the current pair if the threshold is reached and updates the trade record.
        :return: True if trade has been sold/exited_short, False otherwise
        """
        if not trade.is_open:
            raise DependencyException(f'Attempt to handle closed trade: {trade}')

        logger.debug('Handling %s ...', trade)

        (enter, exit_) = (False, False)
        exit_tag = None
        exit_signal_type = "exit_short" if trade.is_short else "exit_long"

        if (self.config.get('use_exit_signal', True) or
                self.config.get('ignore_roi_if_entry_signal', False)):
            analyzed_df, _ = self.dataprovider.get_analyzed_dataframe(trade.pair,
                                                                      self.strategy.timeframe)

            (enter, exit_, exit_tag) = self.strategy.get_exit_signal(
                trade.pair,
                self.strategy.timeframe,
                analyzed_df,
                is_short=trade.is_short
            )

        logger.debug('checking exit')
        exit_rate = await self.exchange.get_rate(
            trade.pair, side='exit', is_short=trade.is_short, refresh=True)
        if await self._check_and_execute_exit(trade, exit_rate, enter, exit_, exit_tag):
            return True

        logger.debug(f'Found no {exit_signal_type} signal for %s.', trade)
        return False

    async def _check_and_execute_exit(self, trade: Trade, exit_rate: float,
                                enter: bool, exit_: bool, exit_tag: Optional[str]) -> bool:
        """
        Check and execute trade exit
        """
        exits: List[ExitCheckTuple] = self.strategy.should_exit(
            trade,
            exit_rate,
            datetime.now(timezone.utc),
            enter=enter,
            exit_=exit_,
            force_stoploss=self.edge.get_stoploss(trade.pair) if self.edge else 0
        )
        for should_exit in exits:
            if should_exit.exit_flag:
                exit_tag1 = exit_tag if should_exit.exit_type == ExitType.EXIT_SIGNAL else None
                logger.info(f'Exit for {trade.pair} detected. Reason: {should_exit.exit_type}'
                            f'{f" Tag: {exit_tag1}" if exit_tag1 is not None else ""}')
                exited = await self.execute_trade_exit(trade, exit_rate, should_exit, exit_tag=exit_tag1)
                if exited:
                    return True
        return False

    async def create_stoploss_order(self, trade: Trade, stop_price: float) -> bool:
        """
        Abstracts creating stoploss orders from the logic.
        Handles errors and updates the trade database object.
        Force-sells the pair (using EmergencySell reason) in case of Problems creating the order.
        :return: True if the order succeeded, and False in case of problems.
        """
        try:
            stoploss_order = await self.exchange.create_stoploss(
                pair=trade.pair,
                amount=trade.amount,
                stop_price=stop_price,
                order_types=self.strategy.order_types,
                side=trade.exit_side,
                leverage=trade.leverage
            )

            order_obj = Order.parse_from_ccxt_object(stoploss_order, trade.pair, 'stoploss',
                                                     trade.amount, stop_price)
            trade.orders.append(order_obj)
            trade.stoploss_order_id = str(stoploss_order['id'])
            trade.stoploss_last_update = datetime.now(timezone.utc)
            return True
        except InsufficientFundsError as e:
            logger.warning(f"Unable to place stoploss order {e}.")
            # Try to figure out what went wrong
            await self.handle_insufficient_funds(trade)

        except InvalidOrderException as e:
            trade.stoploss_order_id = None
            logger.error(f'Unable to place a stoploss order on exchange. {e}')
            logger.warning('Exiting the trade forcefully')
            self.emergency_exit(trade, stop_price)

        except ExchangeError:
            trade.stoploss_order_id = None
            logger.exception('Unable to place a stoploss order on exchange.')
        return False

    async def handle_stoploss_on_exchange(self, trade: Trade) -> bool:
        """
        Check if trade is fulfilled in which case the stoploss
        on exchange should be added immediately if stoploss on exchange
        is enabled.
        # TODO: liquidation price always on exchange, even without stoploss_on_exchange
        # Therefore fetching account liquidations for open pairs may make sense.
        """

        logger.debug('Handling stoploss on exchange %s ...', trade)
        stoploss_order = None

        try:
            # First we check if there is already a stoploss on exchange
            stoploss_order = await self.exchange.fetch_stoploss_order(
                trade.stoploss_order_id, trade.pair) if trade.stoploss_order_id else None
        except InvalidOrderException as exception:
            logger.warning('Unable to fetch stoploss order: %s', exception)

        if stoploss_order:
            await self.update_trade_state(trade, trade.stoploss_order_id, stoploss_order,
                                    stoploss_order=True)

        # We check if stoploss order is fulfilled
        if stoploss_order and stoploss_order['status'] in ('closed', 'triggered'):
            trade.exit_reason = ExitType.STOPLOSS_ON_EXCHANGE.value
            self.update_trade_state(trade, trade.stoploss_order_id, stoploss_order,
                                    stoploss_order=True)
            await self._notify_exit(trade, "stoploss", True)
            self.handle_protections(trade.pair, trade.trade_direction)
            return True

        if trade.has_open_orders or not trade.is_open:
            # Trade has an open Buy or Sell order, Stoploss-handling can't happen in this case
            # as the Amount on the exchange is tied up in another trade.
            # The trade can be closed already (sell-order fill confirmation came in this iteration)
            return False

        # If enter order is fulfilled but there is no stoploss, we add a stoploss on exchange
        if not stoploss_order:
            stop_price = trade.stoploss_or_liquidation
            if self.edge:
                stoploss = self.edge.get_stoploss(pair=trade.pair)
                stop_price = (
                    trade.open_rate * (1 - stoploss) if trade.is_short
                    else trade.open_rate * (1 + stoploss)
                )

            if await self.create_stoploss_order(trade=trade, stop_price=stop_price):
                # The above will return False if the placement failed and the trade was force-sold.
                # in which case the trade will be closed - which we must check below.
                return False

        # If stoploss order is canceled for some reason we add it again
        if (trade.is_open
                and stoploss_order
                and stoploss_order['status'] in ('canceled', 'cancelled')):
            if self.create_stoploss_order(trade=trade, stop_price=trade.stoploss_or_liquidation):
                return False
            else:
                logger.warning('Stoploss order was cancelled, but unable to recreate one.')

        # Finally we check if stoploss on exchange should be moved up because of trailing.
        # Triggered Orders are now real orders - so don't replace stoploss anymore
        if (
            trade.is_open and stoploss_order
            and stoploss_order.get('status_stop') != 'triggered'
            and (self.config.get('trailing_stop', False)
                 or self.config.get('use_custom_stoploss', False))
        ):
            # if trailing stoploss is enabled we check if stoploss value has changed
            # in which case we cancel stoploss order and put another one with new
            # value immediately
            await self.handle_trailing_stoploss_on_exchange(trade, stoploss_order)

        return False

    async def handle_trailing_stoploss_on_exchange(self, trade: Trade, order: Dict) -> None:
        """
        Check to see if stoploss on exchange should be updated
        in case of trailing stoploss on exchange
        :param trade: Corresponding Trade
        :param order: Current on exchange stoploss order
        :return: None
        """
        stoploss_norm = self.exchange.price_to_precision(
            trade.pair, trade.stoploss_or_liquidation,
            rounding_mode=ROUND_DOWN if trade.is_short else ROUND_UP)

        if self.exchange.stoploss_adjust(stoploss_norm, order, side=trade.exit_side):
            # we check if the update is necessary
            update_beat = self.strategy.order_types.get('stoploss_on_exchange_interval', 60)
            upd_req = datetime.now(timezone.utc) - timedelta(seconds=update_beat)
            if trade.stoploss_last_update_utc and upd_req >= trade.stoploss_last_update_utc:
                # cancelling the current stoploss on exchange first
                logger.info(f"Cancelling current stoploss on exchange for pair {trade.pair} "
                            f"(orderid:{order['id']}) in order to add another one ...")

                await self.cancel_stoploss_on_exchange(trade)
                if not trade.is_open:
                    logger.warning(
                        f"Trade {trade} is closed, not creating trailing stoploss order.")
                    return

                # Create new stoploss order
                if not await self.create_stoploss_order(trade=trade, stop_price=stoploss_norm):
                    logger.warning(f"Could not create trailing stoploss order "
                                   f"for pair {trade.pair}.")
    
    async def execute_manage_open_order (self, trade:Trade) :
        open_order: Order
        for open_order in trade.open_orders:
            try:
                order = await self.exchange.fetch_order(open_order.order_id, trade.pair)

            except (ExchangeError):
                logger.info(
                    'Cannot query order for %s due to %s', trade, traceback.format_exc()
                )
                continue

            fully_cancelled = await self.update_trade_state(trade, open_order.order_id, order)
            not_closed = order['status'] == 'open' or fully_cancelled

            if not_closed:
                if (
                    fully_cancelled or (
                        open_order and self.strategy.ft_check_timed_out(
                            trade, open_order, datetime.now(timezone.utc)
                        )
                    )
                ):
                    await self.handle_cancel_order(
                        order, open_order, trade, constants.CANCEL_REASON['TIMEOUT']
                    )
                else:
                    await self.replace_order(order, open_order, trade)


       

    async def handle_cancel_order(self, order: Dict, order_obj: Order, trade: Trade, reason: str) -> None:
        """
        Check if current analyzed order timed out and cancel if necessary.
        :param order: Order dict grabbed with exchange.fetch_order()
        :param order_obj: Order object from the database.
        :param trade: Trade object.
        :return: None
        """
        if order['side'] == trade.entry_side:
            await self.handle_cancel_enter(trade, order, order_obj, reason)
        else:
            canceled = await self.handle_cancel_exit(trade, order, order_obj, reason)
            canceled_count = trade.get_canceled_exit_order_count()
            max_timeouts = self.config.get('unfilledtimeout', {}).get('exit_timeout_count', 0)
            if (canceled and max_timeouts > 0 and canceled_count >= max_timeouts):
                logger.warning(f"Emergency exiting trade {trade}, as the exit order "
                               f"timed out {max_timeouts} times. force selling {order['amount']}.")
                await self.emergency_exit(trade, order['price'], order['amount'])

    async def emergency_exit(
            self, trade: Trade, price: float, sub_trade_amt: Optional[float] = None) -> None:
        try:
            await self.execute_trade_exit(
                trade, price,
                exit_check=ExitCheckTuple(exit_type=ExitType.EMERGENCY_EXIT),
                sub_trade_amt=sub_trade_amt
                )
        except DependencyException as exception:
            logger.warning(
                f'Unable to emergency exit trade {trade.pair}: {exception}')

    async def replace_order_failed(self, trade: Trade, msg: str) -> None:
        """
        Order replacement fail handling.
        Deletes the trade if necessary.
        :param trade: Trade object.
        :param msg: Error message.
        """
        logger.warning(msg)
        if trade.nr_of_successful_entries == 0:
            # this is the first entry and we didn't get filled yet, delete trade
            logger.warning(f"Removing {trade} from database.")
            await self._notify_enter_cancel(
                trade, order_type=self.strategy.order_types['entry'],
                reason=constants.CANCEL_REASON['REPLACE_FAILED'])
            trade.delete()

    async def replace_order(self, order: Dict, order_obj: Optional[Order], trade: Trade) -> None:
        """
        Check if current analyzed entry order should be replaced or simply cancelled.
        To simply cancel the existing order(no replacement) adjust_entry_price() should return None
        To maintain existing order adjust_entry_price() should return order_obj.price
        To replace existing order adjust_entry_price() should return desired price for limit order
        :param order: Order dict grabbed with exchange.fetch_order()
        :param order_obj: Order object.
        :param trade: Trade object.
        :return: None
        """
        analyzed_df, _ = self.dataprovider.get_analyzed_dataframe(trade.pair,
                                                                  self.strategy.timeframe)
        latest_candle_open_date = analyzed_df.iloc[-1]['date'] if len(analyzed_df) > 0 else None
        latest_candle_close_date = timeframe_to_next_date(self.strategy.timeframe,
                                                          latest_candle_open_date)
        # Check if new candle
        if (
            order_obj and order_obj.side == trade.entry_side
            and latest_candle_close_date > order_obj.order_date_utc
        ):
            # New candle
            proposed_rate = await self.exchange.get_rate(
                trade.pair, side='entry', is_short=trade.is_short, refresh=True)
            adjusted_entry_price = strategy_safe_wrapper(
                self.strategy.adjust_entry_price, default_retval=order_obj.safe_placement_price)(
                trade=trade, order=order_obj, pair=trade.pair,
                current_time=datetime.now(timezone.utc), proposed_rate=proposed_rate,
                current_order_rate=order_obj.safe_placement_price, entry_tag=trade.enter_tag,
                side=trade.trade_direction)

            replacing = True
            cancel_reason = constants.CANCEL_REASON['REPLACE']
            if not adjusted_entry_price:
                replacing = False
                cancel_reason = constants.CANCEL_REASON['USER_CANCEL']
            if order_obj.safe_placement_price != adjusted_entry_price:
                # cancel existing order if new price is supplied or None
                res = await self.handle_cancel_enter(trade, order, order_obj, cancel_reason,
                                               replacing=replacing)
                if not res:
                    await self.replace_order_failed(
                        trade, f"Could not cancel order for {trade}, therefore not replacing.")
                    return
                if adjusted_entry_price:
                    # place new order only if new price is supplied
                    try:
                        if not await self.execute_entry(
                            pair=trade.pair,
                            stake_amount=(
                                order_obj.safe_remaining * order_obj.safe_price / trade.leverage),
                            price=adjusted_entry_price,
                            trade=trade,
                            is_short=trade.is_short,
                            mode='replace',
                        ):
                            await self.replace_order_failed(
                                trade, f"Could not replace order for {trade}.")
                    except DependencyException as exception:
                        logger.warning(
                            f'Unable to replace order for {trade.pair}: {exception}')
                        await self.replace_order_failed(trade, f"Could not replace order for {trade}.")

    async def cancel_all_open_orders(self) -> None:
        """
        Cancel all orders that are currently open
        :return: None
        """

        for trade in  Trade.get_open_trades():
            for open_order in trade.open_orders:
                try:
                    order = await self.exchange.fetch_order(open_order.order_id, trade.pair)
                except (ExchangeError):
                    logger.info("Can't query order for %s due to %s", trade, traceback.format_exc())
                    continue

                if order['side'] == trade.entry_side:
                    await self.handle_cancel_enter(
                        trade, order, open_order, constants.CANCEL_REASON['ALL_CANCELLED']
                    )

                elif order['side'] == trade.exit_side:
                    await self.handle_cancel_exit(
                        trade, order, open_order, constants.CANCEL_REASON['ALL_CANCELLED']
                    )
        Trade.commit()

    async def handle_cancel_enter(
            self, trade: Trade, order: Dict, order_obj: Order,
            reason: str, replacing: Optional[bool] = False
    ) -> bool:
        """
        entry cancel - cancel order
        :param order_obj: Order object from the database.
        :param replacing: Replacing order - prevent trade deletion.
        :return: True if trade was fully cancelled
        """
        was_trade_fully_canceled = False
        order_id = order_obj.order_id
        side = trade.entry_side.capitalize()

        if order['status'] not in constants.NON_OPEN_EXCHANGE_STATES:
            filled_val: float = order.get('filled', 0.0) or 0.0
            filled_stake = filled_val * trade.open_rate
            minstake = self.exchange.get_min_pair_stake_amount(
                trade.pair, trade.open_rate, self.strategy.stoploss)

            if filled_val > 0 and minstake and filled_stake < minstake:
                logger.warning(
                    f"Order {order_id} for {trade.pair} not cancelled, "
                    f"as the filled amount of {filled_val} would result in an unexitable trade.")
                return False
            corder = await self.exchange.cancel_order_with_result(order_id, trade.pair, trade.amount)
            order_obj.ft_cancel_reason = reason
            # if replacing, retry fetching the order 3 times if the status is not what we need
            if replacing:
                retry_count = 0
                while (
                    corder.get('status') not in constants.NON_OPEN_EXCHANGE_STATES
                    and retry_count < 3
                ):
                    sleep(0.5)
                    corder = await self.exchange.fetch_order(order_id, trade.pair)
                    retry_count += 1

            # Avoid race condition where the order could not be cancelled coz its already filled.
            # Simply bailing here is the only safe way - as this order will then be
            # handled in the next iteration.
            if corder.get('status') not in constants.NON_OPEN_EXCHANGE_STATES:
                logger.warning(f"Order {order_id} for {trade.pair} not cancelled.")
                return False
        else:
            # Order was cancelled already, so we can reuse the existing dict
            corder = order
            if order_obj.ft_cancel_reason is None:
                order_obj.ft_cancel_reason = constants.CANCEL_REASON['CANCELLED_ON_EXCHANGE']

        logger.info(f'{side} order {order_obj.ft_cancel_reason} for {trade}.')

        # Using filled to determine the filled amount
        filled_amount = safe_value_fallback2(corder, order, 'filled', 'filled')
        if isclose(filled_amount, 0.0, abs_tol=constants.MATH_CLOSE_PREC):
            was_trade_fully_canceled = True
            # if trade is not partially completed and it's the only order, just delete the trade
            open_order_count = len([
                order for order in trade.orders if order.ft_is_open and order.order_id != order_id
                ])
            if open_order_count < 1 and trade.nr_of_successful_entries == 0 and not replacing:
                logger.info(f'{side} order fully cancelled. Removing {trade} from database.')
                trade.delete()
                order_obj.ft_cancel_reason += f", {constants.CANCEL_REASON['FULLY_CANCELLED']}"
            else:
                await self.update_trade_state(trade, order_id, corder)
                logger.info(f'{side} Order timeout for {trade}.')
        else:
            # update_trade_state (and subsequently recalc_trade_from_orders) will handle updates
            # to the trade object
            await self.update_trade_state(trade, order_id, corder)

            logger.info(f'Partial {trade.entry_side} order timeout for {trade}.')
            order_obj.ft_cancel_reason += f", {constants.CANCEL_REASON['PARTIALLY_FILLED']}"

        await self.wallets.update()
        self._notify_enter_cancel(trade, order_type=self.strategy.order_types['entry'],
                                  reason=order_obj.ft_cancel_reason)
        return was_trade_fully_canceled

    async def handle_cancel_exit(
        self, trade: Trade, order: Dict, order_obj: Order, reason: str
    ) -> bool:
        """
        exit order cancel - cancel order and update trade
        :return: True if exit order was cancelled, false otherwise
        """
        order_id = order_obj.order_id
        cancelled = False
        # Cancelled orders may have the status of 'canceled' or 'closed'
        if order['status'] not in constants.NON_OPEN_EXCHANGE_STATES:
            filled_amt: float = order.get('filled', 0.0) or 0.0
            # Filled val is in quote currency (after leverage)
            filled_rem_stake = trade.stake_amount - (filled_amt * trade.open_rate / trade.leverage)
            minstake = self.exchange.get_min_pair_stake_amount(
                trade.pair, trade.open_rate, self.strategy.stoploss)
            # Double-check remaining amount
            if filled_amt > 0:
                reason = constants.CANCEL_REASON['PARTIALLY_FILLED']
                if minstake and filled_rem_stake < minstake:
                    logger.warning(
                        f"Order {order_id} for {trade.pair} not cancelled, as "
                        f"the filled amount of {filled_amt} would result in an unexitable trade.")
                    reason = constants.CANCEL_REASON['PARTIALLY_FILLED_KEEP_OPEN']

                    await self._notify_exit_cancel(
                        trade,
                        order_type=self.strategy.order_types['exit'],
                        reason=reason, order_id=order['id'],
                        sub_trade=trade.amount != order['amount']
                    )
                    return False
            order_obj.ft_cancel_reason = reason
            try:
                order = await self.exchange.cancel_order_with_result(
                    order['id'], trade.pair, trade.amount)
            except InvalidOrderException:
                logger.exception(
                    f"Could not cancel {trade.exit_side} order {order_id}")
                return False

            # Set exit_reason for fill message
            exit_reason_prev = trade.exit_reason
            trade.exit_reason = trade.exit_reason + f", {reason}" if trade.exit_reason else reason
            # Order might be filled above in odd timing issues.
            if order.get('status') in ('canceled', 'cancelled'):
                trade.exit_reason = None
            else:
                trade.exit_reason = exit_reason_prev
            cancelled = True
        else:
            if order_obj.ft_cancel_reason is None:
                order_obj.ft_cancel_reason = constants.CANCEL_REASON['CANCELLED_ON_EXCHANGE']
            trade.exit_reason = None

        await self.update_trade_state(trade, order['id'], order)

        logger.info(
            f'{trade.exit_side.capitalize()} order {order_obj.ft_cancel_reason} for {trade}.')
        trade.close_rate = None
        trade.close_rate_requested = None

        self._notify_exit_cancel(
            trade,
            order_type=self.strategy.order_types['exit'],
            reason=order_obj.ft_cancel_reason, order_id=order['id'],
            sub_trade=trade.amount != order['amount']
        )
        return cancelled

    async def _safe_exit_amount(self, trade: Trade, pair: str, amount: float) -> float:
        """
        Get sellable amount.
        Should be trade.amount - but will fall back to the available amount if necessary.
        This should cover cases where get_real_amount() was not able to update the amount
        for whatever reason.
        :param trade: Trade we're working with
        :param pair: Pair we're trying to sell
        :param amount: amount we expect to be available
        :return: amount to sell
        :raise: DependencyException: if available balance is not within 2% of the available amount.
        """
        # Update wallets to ensure amounts tied up in a stoploss is now free!
        await self.wallets.update()
        if self.trading_mode == TradingMode.FUTURES:
            # A safe exit amount isn't needed for futures, you can just exit/close the position
            return amount

        trade_base_currency = self.exchange.get_pair_base_currency(pair)
        wallet_amount = self.wallets.get_free(trade_base_currency)
        logger.debug(f"{pair} - Wallet: {wallet_amount} - Trade-amount: {amount}")
        if wallet_amount >= amount:
            return amount
        elif wallet_amount > amount * 0.98:
            logger.info(f"{pair} - Falling back to wallet-amount {wallet_amount} -> {amount}.")
            trade.amount = wallet_amount
            return wallet_amount
        else:
            raise DependencyException(
                f"Not enough amount to exit trade. Trade-amount: {amount}, Wallet: {wallet_amount}")

    async def execute_trade_exit(
            self,
            trade: Trade,
            limit: float,
            exit_check: ExitCheckTuple,
            *,
            exit_tag: Optional[str] = None,
            ordertype: Optional[str] = None,
            sub_trade_amt: Optional[float] = None,
    ) -> bool:
        """
        Executes a trade exit for the given trade and limit
        :param trade: Trade instance
        :param limit: limit rate for the sell order
        :param exit_check: CheckTuple with signal and reason
        :return: True if it succeeds False
        """
        trade.set_funding_fees(
            await self.exchange.get_funding_fees(
                pair=trade.pair,
                amount=trade.amount,
                is_short=trade.is_short,
                open_date=trade.date_last_filled_utc)
        )

        exit_type = 'exit'
        exit_reason = exit_tag or exit_check.exit_reason
        if exit_check.exit_type in (
                ExitType.STOP_LOSS, ExitType.TRAILING_STOP_LOSS, ExitType.LIQUIDATION):
            exit_type = 'stoploss'

        # set custom_exit_price if available
        proposed_limit_rate = limit
        current_profit = trade.calc_profit_ratio(limit)
        custom_exit_price = strategy_safe_wrapper(self.strategy.custom_exit_price,
                                                  default_retval=proposed_limit_rate)(
            pair=trade.pair, trade=trade,
            current_time=datetime.now(timezone.utc),
            proposed_rate=proposed_limit_rate, current_profit=current_profit,
            exit_tag=exit_reason)

        limit = self.get_valid_price(custom_exit_price, proposed_limit_rate)

        # First cancelling stoploss on exchange ...
        trade = await self.cancel_stoploss_on_exchange(trade)

        order_type = ordertype or self.strategy.order_types[exit_type]
        if exit_check.exit_type == ExitType.EMERGENCY_EXIT:
            # Emergency sells (default to market!)
            order_type = self.strategy.order_types.get("emergency_exit", "market")

        amount = await self._safe_exit_amount(trade, trade.pair, sub_trade_amt or trade.amount)
        time_in_force = self.strategy.order_time_in_force['exit']

        if (exit_check.exit_type != ExitType.LIQUIDATION
                and not sub_trade_amt
                and not strategy_safe_wrapper(
                    self.strategy.confirm_trade_exit, default_retval=True)(
                    pair=trade.pair, trade=trade, order_type=order_type, amount=amount, rate=limit,
                    time_in_force=time_in_force, exit_reason=exit_reason,
                    sell_reason=exit_reason,  # sellreason -> compatibility
                    current_time=datetime.now(timezone.utc))):
            logger.info(f"User denied exit for {trade.pair}.")
            return False

        try:
            # Execute sell and update trade record
            order = await self.exchange.create_order(
                pair=trade.pair,
                ordertype=order_type,
                side=trade.exit_side,
                amount=amount,
                rate=limit,
                leverage=trade.leverage,
                reduceOnly=self.trading_mode == TradingMode.FUTURES,
                time_in_force=time_in_force
            )
        except InsufficientFundsError as e:
            logger.warning(f"Unable to place order {e}.")
            # Try to figure out what went wrong
            await self.handle_insufficient_funds(trade)
            return False

        order_obj = Order.parse_from_ccxt_object(order, trade.pair, trade.exit_side, amount, limit)
        trade.orders.append(order_obj)

        trade.exit_order_status = ''
        trade.close_rate_requested = limit
        trade.exit_reason = exit_reason

        await self._notify_exit(trade, order_type, sub_trade=bool(sub_trade_amt), order=order_obj)
        # In case of market sell orders the order can be closed immediately
        if order.get('status', 'unknown') in ('closed', 'expired'):
            await self.update_trade_state(trade, order_obj.order_id, order)
        Trade.commit()

        return True

    async def _notify_exit(self, trade: Trade, order_type: str, fill: bool = False,
                     sub_trade: bool = False, order: Optional[Order] = None) -> None:
        """
        Sends rpc notification when a sell occurred.
        """
        # Use cached rates here - it was updated seconds ago.
        current_rate = await self.exchange.get_rate(
            trade.pair, side='exit', is_short=trade.is_short, refresh=False) if not fill else None

        # second condition is for mypy only; order will always be passed during sub trade
        if sub_trade and order is not None:
            amount = order.safe_filled if fill else order.safe_amount
            order_rate: float = order.safe_price

            profit = trade.calculate_profit(order_rate, amount, trade.open_rate)
        else:
            order_rate = trade.safe_close_rate
            profit = trade.calculate_profit(rate=order_rate)
            amount = trade.amount
        gain: ProfitLossStr = "profit" if profit.profit_ratio > 0 else "loss"

        msg: RPCExitMsg = {
            'type': (RPCMessageType.EXIT_FILL if fill
                     else RPCMessageType.EXIT),
            'trade_id': trade.id,
            'exchange': trade.exchange.capitalize(),
            'pair': trade.pair,
            'leverage': trade.leverage,
            'direction': 'Short' if trade.is_short else 'Long',
            'gain': gain,
            'limit': order_rate,  # Deprecated
            'order_rate': order_rate,
            'order_type': order_type,
            'amount': amount,
            'open_rate': trade.open_rate,
            'close_rate': order_rate,
            'current_rate': current_rate,
            'profit_amount': profit.profit_abs,
            'profit_ratio': profit.profit_ratio,
            'buy_tag': trade.enter_tag,
            'enter_tag': trade.enter_tag,
            'exit_reason': trade.exit_reason,
            'open_date': trade.open_date_utc,
            'close_date': trade.close_date_utc or datetime.now(timezone.utc),
            'stake_amount': trade.stake_amount,
            'stake_currency': self.config['stake_currency'],
            'base_currency': self.exchange.get_pair_base_currency(trade.pair),
            'quote_currency': self.exchange.get_pair_quote_currency(trade.pair),
            'fiat_currency': self.config.get('fiat_display_currency'),
            'sub_trade': sub_trade,
            'cumulative_profit': trade.realized_profit,
            'final_profit_ratio': trade.close_profit if not trade.is_open else None,
            'is_final_exit': trade.is_open is False,
        }

        # Send the message
        self.rpc.send_msg(msg)

    async def _notify_exit_cancel(self, trade: Trade, order_type: str, reason: str,
                            order_id: str, sub_trade: bool = False) -> None:
        """
        Sends rpc notification when a sell cancel occurred.
        """
        if trade.exit_order_status == reason:
            return
        else:
            trade.exit_order_status = reason

        order_or_none = trade.select_order_by_order_id(order_id)
        order = self.order_obj_or_raise(order_id, order_or_none)

        profit_rate: float = trade.safe_close_rate
        profit = trade.calculate_profit(rate=profit_rate)
        current_rate = await self.exchange.get_rate(
            trade.pair, side='exit', is_short=trade.is_short, refresh=False)
        gain: ProfitLossStr = "profit" if profit.profit_ratio > 0 else "loss"

        msg: RPCExitCancelMsg = {
            'type': RPCMessageType.EXIT_CANCEL,
            'trade_id': trade.id,
            'exchange': trade.exchange.capitalize(),
            'pair': trade.pair,
            'leverage': trade.leverage,
            'direction': 'Short' if trade.is_short else 'Long',
            'gain': gain,
            'limit': profit_rate or 0,
            'order_type': order_type,
            'amount': order.safe_amount_after_fee,
            'open_rate': trade.open_rate,
            'current_rate': current_rate,
            'profit_amount': profit.profit_abs,
            'profit_ratio': profit.profit_ratio,
            'buy_tag': trade.enter_tag,
            'enter_tag': trade.enter_tag,
            'exit_reason': trade.exit_reason,
            'open_date': trade.open_date,
            'close_date': trade.close_date or datetime.now(timezone.utc),
            'stake_currency': self.config['stake_currency'],
            'base_currency': self.exchange.get_pair_base_currency(trade.pair),
            'quote_currency': self.exchange.get_pair_quote_currency(trade.pair),
            'fiat_currency': self.config.get('fiat_display_currency', None),
            'reason': reason,
            'sub_trade': sub_trade,
            'stake_amount': trade.stake_amount,
        }

        # Send the message
        self.rpc.send_msg(msg)

    def order_obj_or_raise(self, order_id: str, order_obj: Optional[Order]) -> Order:
        if not order_obj:
            raise DependencyException(
                f"Order_obj not found for {order_id}. This should not have happened.")
        return order_obj

#
# Common update trade state methods
#

    async def update_trade_state(
            self, trade: Trade, order_id: Optional[str],
            action_order: Optional[Dict[str, Any]] = None, *,
            stoploss_order: bool = False, send_msg: bool = True) -> bool:
        """
        Checks trades with open orders and updates the amount if necessary
        Handles closing both buy and sell orders.
        :param trade: Trade object of the trade we're analyzing
        :param order_id: Order-id of the order we're analyzing
        :param action_order: Already acquired order object
        :param send_msg: Send notification - should always be True except in "recovery" methods
        :return: True if order has been cancelled without being filled partially, False otherwise
        """
        if not order_id:
            logger.warning(f'Orderid for trade {trade} is empty.')
            return False

        # Update trade with order values
        if not stoploss_order:
            logger.info(f'Found open order for {trade}')
        try:
            order = action_order or await self.exchange.fetch_order_or_stoploss_order(
                order_id, trade.pair, stoploss_order)
        except InvalidOrderException as exception:
            logger.warning('Unable to fetch order %s: %s', order_id, exception)
            return False

        trade.update_order(order)

        if self.exchange.check_order_canceled_empty(order):
            # Trade has been cancelled on exchange
            # Handling of this will happen in handle_cancel_order.
            return True

        order_obj_or_none = trade.select_order_by_order_id(order_id)
        order_obj = self.order_obj_or_raise(order_id, order_obj_or_none)

        await self.handle_order_fee(trade, order_obj, order)

        trade.update_trade(order_obj, not send_msg)

        trade = await self._update_trade_after_fill(trade, order_obj)
        Trade.commit()

        await self.order_close_notify(trade, order_obj, stoploss_order, send_msg)

        return False

    async def _update_trade_after_fill(self, trade: Trade, order: Order) -> Trade:
        if order.status in constants.NON_OPEN_EXCHANGE_STATES:
            # If a entry order was closed, force update on stoploss on exchange
            if order.ft_order_side == trade.entry_side:
                trade = await self.cancel_stoploss_on_exchange(trade)
                if not self.edge:
                    # TODO: should shorting/leverage be supported by Edge,
                    # then this will need to be fixed.
                    trade.adjust_stop_loss(trade.open_rate, self.strategy.stoploss, initial=True)
            if order.ft_order_side == trade.entry_side or (trade.amount > 0 and trade.is_open):
                # Must also run for partial exits
                # TODO: Margin will need to use interest_rate as well.
                # interest_rate = self.exchange.get_interest_rate()
                try:
                    trade.set_liquidation_price(await self.exchange.get_liquidation_price(
                        pair=trade.pair,
                        open_rate=trade.open_rate,
                        is_short=trade.is_short,
                        amount=trade.amount,
                        stake_amount=trade.stake_amount,
                        leverage=trade.leverage,
                        wallet_balance=trade.stake_amount,
                    ))
                except DependencyException:
                    logger.warning('Unable to calculate liquidation price')
                if self.strategy.use_custom_stoploss:
                    current_rate = await self.exchange.get_rate(
                        trade.pair, side='exit', is_short=trade.is_short, refresh=True)
                    profit = trade.calc_profit_ratio(current_rate)
                    self.strategy.ft_stoploss_adjust(current_rate, trade,
                                                     datetime.now(timezone.utc), profit, 0,
                                                     after_fill=True)
            # Updating wallets when order is closed
            await self.wallets.update()
        return trade

    async def order_close_notify(
            self, trade: Trade, order: Order, stoploss_order: bool, send_msg: bool):
        """send "fill" notifications"""

        if order.ft_order_side == trade.exit_side:
            # Exit notification
            if send_msg and not stoploss_order and order.order_id not in trade.open_orders_ids:
                await self._notify_exit(trade, order.order_type, fill=True,
                                  sub_trade=trade.is_open, order=order)
            if not trade.is_open:
                self.handle_protections(trade.pair, trade.trade_direction)
        elif send_msg and order.order_id not in trade.open_orders_ids and not stoploss_order:
            sub_trade = not isclose(order.safe_amount_after_fee,
                                    trade.amount, abs_tol=constants.MATH_CLOSE_PREC)
            # Enter fill
            await self._notify_enter(trade, order, order.order_type, fill=True, sub_trade=sub_trade)

    def handle_protections(self, pair: str, side: LongShort) -> None:
        # Lock pair for one candle to prevent immediate rebuys
        self.strategy.lock_pair(pair, datetime.now(timezone.utc), reason='Auto lock')
        prot_trig = self.protections.stop_per_pair(pair, side=side)
        if prot_trig:
            msg: RPCProtectionMsg = {
                'type': RPCMessageType.PROTECTION_TRIGGER,
                'base_currency': self.exchange.get_pair_base_currency(prot_trig.pair),
                **prot_trig.to_json()  # type: ignore
            }
            self.rpc.send_msg(msg)

        prot_trig_glb = self.protections.global_stop(side=side)
        if prot_trig_glb:
            msg = {
                'type': RPCMessageType.PROTECTION_TRIGGER_GLOBAL,
                'base_currency': self.exchange.get_pair_base_currency(prot_trig_glb.pair),
                **prot_trig_glb.to_json()  # type: ignore
            }
            self.rpc.send_msg(msg)

    async def apply_fee_conditional(self, trade: Trade, trade_base_currency: str,
                              amount: float, fee_abs: float, order_obj: Order) -> Optional[float]:
        """
        Applies the fee to amount (either from Order or from Trades).
        Can eat into dust if more than the required asset is available.
        In case of trade adjustment orders, trade.amount will not have been adjusted yet.
        Can't happen in Futures mode - where Fees are always in settlement currency,
        never in base currency.
        """
        await self.wallets.update()
        amount_ = trade.amount
        if order_obj.ft_order_side == trade.exit_side or order_obj.ft_order_side == 'stoploss':
            # check against remaining amount!
            amount_ = trade.amount - amount

        if trade.nr_of_successful_entries >= 1 and order_obj.ft_order_side == trade.entry_side:
            # In case of rebuy's, trade.amount doesn't contain the amount of the last entry.
            amount_ = trade.amount + amount

        if fee_abs != 0 and self.wallets.get_free(trade_base_currency) >= amount_:
            # Eat into dust if we own more than base currency
            logger.info(f"Fee amount for {trade} was in base currency - "
                        f"Eating Fee {fee_abs} into dust.")
        elif fee_abs != 0:
            logger.info(f"Applying fee on amount for {trade}, fee={fee_abs}.")
            return fee_abs
        return None

    async def handle_order_fee(self, trade: Trade, order_obj: Order, order: Dict[str, Any]) -> None:
        # Try update amount (binance-fix)
        try:
            fee_abs = await self.get_real_amount(trade, order, order_obj)
            if fee_abs is not None:
                order_obj.ft_fee_base = fee_abs
        except DependencyException as exception:
            logger.warning("Could not update trade amount: %s", exception)

    async def get_real_amount(self, trade: Trade, order: Dict, order_obj: Order) -> Optional[float]:
        """
        Detect and update trade fee.
        Calls trade.update_fee() upon correct detection.
        Returns modified amount if the fee was taken from the destination currency.
        Necessary for exchanges which charge fees in base currency (e.g. binance)
        :return: Absolute fee to apply for this order or None
        """
        # Init variables
        order_amount = safe_value_fallback(order, 'filled', 'amount')
        # Only run for closed orders
        if (
            trade.fee_updated(order.get('side', ''))
            or order['status'] == 'open'
            or order_obj.ft_fee_base
        ):
            return None

        trade_base_currency = self.exchange.get_pair_base_currency(trade.pair)
        # use fee from order-dict if possible
        if self.exchange.order_has_fee(order):
            fee_cost, fee_currency, fee_rate = await self.exchange.extract_cost_curr_rate(
                order['fee'], order['symbol'], order['cost'], order_obj.safe_filled)
            logger.info(f"Fee for Trade {trade} [{order_obj.ft_order_side}]: "
                        f"{fee_cost:.8g} {fee_currency} - rate: {fee_rate}")
            if fee_rate is None or fee_rate < 0.02:
                # Reject all fees that report as > 2%.
                # These are most likely caused by a parsing bug in ccxt
                # due to multiple trades (https://github.com/ccxt/ccxt/issues/8025)
                trade.update_fee(fee_cost, fee_currency, fee_rate, order.get('side', ''))
                if trade_base_currency == fee_currency:
                    # Apply fee to amount
                    return self.apply_fee_conditional(trade, trade_base_currency,
                                                      amount=order_amount, fee_abs=fee_cost,
                                                      order_obj=order_obj)
                return None
        return await self.fee_detection_from_trades(
            trade, order, order_obj, order_amount, order.get('trades', []))

    async def fee_detection_from_trades(self, trade: Trade, order: Dict, order_obj: Order,
                                  order_amount: float, trades: List) -> Optional[float]:
        """
        fee-detection fallback to Trades.
        Either uses provided trades list or the result of fetch_my_trades to get correct fee.
        """
        if not trades:
            trades = await self.exchange.get_trades_for_order(
                self.exchange.get_order_id_conditional(order), trade.pair, order_obj.order_date)

        if len(trades) == 0:
            logger.info("Applying fee on amount for %s failed: myTrade-Dict empty found", trade)
            return None
        fee_currency = None
        amount = 0
        fee_abs = 0.0
        fee_cost = 0.0
        trade_base_currency = self.exchange.get_pair_base_currency(trade.pair)
        fee_rate_array: List[float] = []
        for exectrade in trades:
            amount += exectrade['amount']
            if self.exchange.order_has_fee(exectrade):
                # Prefer singular fee
                fees = [exectrade['fee']]
            else:
                fees = exectrade.get('fees', [])
            for fee in fees:

                fee_cost_, fee_currency, fee_rate_ = await self.exchange.extract_cost_curr_rate(
                    fee, exectrade['symbol'], exectrade['cost'], exectrade['amount']
                )
                fee_cost += fee_cost_
                if fee_rate_ is not None:
                    fee_rate_array.append(fee_rate_)
                # only applies if fee is in quote currency!
                if trade_base_currency == fee_currency:
                    fee_abs += fee_cost_
        # Ensure at least one trade was found:
        if fee_currency:
            # fee_rate should use mean
            fee_rate = sum(fee_rate_array) / float(len(fee_rate_array)) if fee_rate_array else None
            if fee_rate is not None and fee_rate < 0.02:
                # Only update if fee-rate is < 2%
                trade.update_fee(fee_cost, fee_currency, fee_rate, order.get('side', ''))
            else:
                logger.warning(
                    f"Not updating {order.get('side', '')}-fee - rate: {fee_rate}, {fee_currency}.")

        if not isclose(amount, order_amount, abs_tol=constants.MATH_CLOSE_PREC):
            # * Leverage could be a cause for this warning
            logger.warning(f"Amount {amount} does not ma  tch amount {trade.amount}")
            raise DependencyException("Half bought? Amounts don't match")

        if fee_abs != 0:
            return self.apply_fee_conditional(
                trade, trade_base_currency, amount=amount, fee_abs=fee_abs, order_obj=order_obj)
        return None

    def get_valid_price(self, custom_price: float, proposed_price: float) -> float:
        """
        Return the valid price.
        Check if the custom price is of the good type if not return proposed_price
        :return: valid price for the order
        """
        if custom_price:
            try:
                valid_custom_price = float(custom_price)
            except ValueError:
                valid_custom_price = proposed_price
        else:
            valid_custom_price = proposed_price

        cust_p_max_dist_r = self.config.get('custom_price_max_distance_ratio', 0.02)
        min_custom_price_allowed = proposed_price - (proposed_price * cust_p_max_dist_r)
        max_custom_price_allowed = proposed_price + (proposed_price * cust_p_max_dist_r)

        # Bracket between min_custom_price_allowed and max_custom_price_allowed
        return max(
            min(valid_custom_price, max_custom_price_allowed),
            min_custom_price_allowed)
