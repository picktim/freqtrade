import ccxt
import  ccxt.async_support
import ccxt.pro

class Binance_Sync (ccxt.binance) :
    def __init__(self, config=...):
          super().__init__(config)
          
    def  parse_ohlcv(self, ohlcv, market=None):
            #
            #     [
            #         1591478520000,  # open time
            #         "0.02501300",   # open
            #         "0.02501800",   # high
            #         "0.02500000",   # low
            #         "0.02500000",   # close
            #         "22.19000000",  # volume
            #         1591478579999,  # close time
            #         "0.55490906",   # quote asset volume
            #         40,             # number of trades
            #         "10.92900000",  # taker buy base asset volume
            #         "0.27336462",   # taker buy quote asset volume
            #         "0"             # ignore
            #     ]
            #
            return [
                self.safe_integer(ohlcv, 0),
                self.safe_number(ohlcv, 1),
                self.safe_number(ohlcv, 2),
                self.safe_number(ohlcv, 3),
                self.safe_number(ohlcv, 4),
                self.safe_number(ohlcv, 5),
                self.safe_number(ohlcv, 9),  # << here
            ]
class Binance_Async (ccxt.async_support.binance) :
    def __init__(self, config=...):
          super().__init__(config)
          
    def  parse_ohlcv(self, ohlcv, market=None):
            #
            #     [
            #         1591478520000,  # open time
            #         "0.02501300",   # open
            #         "0.02501800",   # high
            #         "0.02500000",   # low
            #         "0.02500000",   # close
            #         "22.19000000",  # volume
            #         1591478579999,  # close time
            #         "0.55490906",   # quote asset volume
            #         40,             # number of trades
            #         "10.92900000",  # taker buy base asset volume
            #         "0.27336462",   # taker buy quote asset volume
            #         "0"             # ignore
            #     ]
            #
            return [
                self.safe_integer(ohlcv, 0),
                self.safe_number(ohlcv, 1),
                self.safe_number(ohlcv, 2),
                self.safe_number(ohlcv, 3),
                self.safe_number(ohlcv, 4),
                self.safe_number(ohlcv, 5),
                self.safe_number(ohlcv, 9),  # << here
            ]
class Binance_Pro (ccxt.pro.binance) :
    def __init__(self, config=...):
          super().__init__(config)
          
    def  parse_ohlcv(self, ohlcv, market=None):
            #
            #     [
            #         1591478520000,  # open time
            #         "0.02501300",   # open
            #         "0.02501800",   # high
            #         "0.02500000",   # low
            #         "0.02500000",   # close
            #         "22.19000000",  # volume
            #         1591478579999,  # close time
            #         "0.55490906",   # quote asset volume
            #         40,             # number of trades
            #         "10.92900000",  # taker buy base asset volume
            #         "0.27336462",   # taker buy quote asset volume
            #         "0"             # ignore
            #     ]
            #
            return [
                self.safe_integer(ohlcv, 0),
                self.safe_number(ohlcv, 1),
                self.safe_number(ohlcv, 2),
                self.safe_number(ohlcv, 3),
                self.safe_number(ohlcv, 4),
                self.safe_number(ohlcv, 5),
                self.safe_number(ohlcv, 9),  # << here
            ]