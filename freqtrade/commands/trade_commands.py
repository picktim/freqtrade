import asyncio
import logging
import signal
from typing import Any, Dict

import uvloop


logger = logging.getLogger(__name__)


def start_trading(args: Dict[str, Any]) -> int:
    """
    Main entry point for trading mode
    """
    # Import here to avoid loading worker module when it's not used
    # from freqtrade.worker import Worker

    # def term_handler(signum, frame):
    #     # Raise KeyboardInterrupt - so we can handle it in the same way as Ctrl-C
    #     raise KeyboardInterrupt()

    # Create and run worker
    # worker = None
    uvloop.install()
    uvloop.run(runfreq(args))
    # try:
    #     signal.signal(signal.SIGTERM, term_handler)
    #     _loop = asyncio.get_event_loop()
    #     worker = Worker(args,  loop= _loop)
    #     uvloop.run(worker.run())

    # except Exception as e:
    #     logger.error(str(e))
    #     logger.exception("Fatal exception!")
    # except (KeyboardInterrupt):
    #     logger.info('SIGINT received, aborting ...')
    # finally:
    #     if worker:
    #         logger.info("worker found ... calling exit")
    #         asyncio.run (worker.exit())
    return 0



async def runfreq (args : Dict[str, Any]) :
        from freqtrade.worker import Worker

        def term_handler(signum, frame):
        # Raise KeyboardInterrupt - so we can handle it in the same way as Ctrl-C
            raise KeyboardInterrupt()
        # Create and run worker
        worker = None
        try:
            signal.signal(signal.SIGTERM, term_handler)
            loop = asyncio.get_running_loop()
            worker = Worker(args, loop= loop)
            await worker.run()
        except Exception as e:
            logger.error(str(e))
            logger.exception("Fatal exception!")
        except (KeyboardInterrupt):
            logger.info('SIGINT received, aborting ...')
        finally:
            if worker:
                logger.info("worker found ... calling exit")
                await worker.exit()
        return 0
