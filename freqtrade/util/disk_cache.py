"""Contains caching utilities."""

"""Copyright (C) 2023 Edward West. All rights reserved.

This code is licensed under Apache 2.0 with Commons Clause license
(see LICENSE for details).
"""

import os
import asyncio
# from pybroker.scope import StaticScope
from dataclasses import dataclass
from datetime import datetime
from diskcache import Cache
from typing import Final, Optional
from freqtrade.constants import Config

_DEFAULT_CACHE_DIRNAME: Final = ".freqcache"


@dataclass(frozen=True)
class EntityCacheKey:
    """Cache key used for  entity."""

    symbol: str
    exchange: str

class EntityCache :
    __instance  = None

    def __init__(self, cache_dir:str) -> None:

        if EntityCache.__instance != None :
            raise Exception("This class is a singleton!")
        else :
            self.cache :Cache  = Cache(cache_dir)
            EntityCache.__instance = self
            
       
    def close (self) -> None :
        self.cache.close()

    def setCache(self, key : str, value: any) : 
        self.cache[key] = value

    def remove (self, key: str) :
        del self.cache[key]
        
    async def set_async(self, key, val):
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, self.cache.set, key, val)
        result = await future
        return result
    

    @staticmethod
    def instance (config: Config = None) -> None :
        if EntityCache.__instance is None:
            user_dir = f"{config['user_data_dir']}/{'cache'}" 
            exchange = config['exchange'].get('name')
            candel_type = config["trading_mode"]
            cache_dir = _get_cache_dir(user_dir, exchange, candel_type)
            EntityCache.__instance = EntityCache(cache_dir)
        return EntityCache.__instance
    



def _get_cache_dir(
    cache_dir: Optional[str], namespace: str, sub_dir: str
) -> str:
    if not namespace:
        raise ValueError("Cache namespace cannot be empty.")
    base_dir = (
        os.path.join(os.getcwd(), _DEFAULT_CACHE_DIRNAME)
        if cache_dir is None
        else cache_dir
    )
    return os.path.join(base_dir, namespace, sub_dir)


def enable_data_source_cache(
    namespace: str, cache_dir: Optional[str] = None
) -> Cache:
    r"""Enables caching of data retrieved from
    :class:`pybroker.data.DataSource`\ s.

    Args:
        namespace: Namespace of the cache.
        cache_dir: Directory used to store cached data.

    Returns:
        :class:`diskcache.Cache` instance.
    """
    # scope = StaticScope.instance()
    cache_dir = _get_cache_dir(cache_dir, namespace, "data_source")
    # scope.data_source_cache_ns = namespace
    cache = Cache(directory=cache_dir)
    # scope.data_source_cache = cache
    # scope.logger.debug_enable_data_source_cache(namespace, cache_dir)
    return cache


def disable_data_source_cache():
    r"""Disables caching data retrieved from
    :class:`pybroker.data.DataSource`\ s.
    """
    # scope = StaticScope.instance()
    # scope.data_source_cache = None
    # scope.data_source_cache_ns = ""
    # scope.logger.debug_disable_data_source_cache()


def clear_data_source_cache():
    r"""Clears data cached from :class:`pybroker.data.DataSource`\ s.
    :meth:`enable_data_source_cache` must be called first before clearing.
    """
    # scope = StaticScope.instance()
    # cache = scope.data_source_cache
    # if cache is None:
    #     raise ValueError(
    #         "Data source cache needs to be enabled before clearing."
    #     )
    # cache.clear()
    # scope.logger.debug_clear_data_source_cache(cache.directory)


def enable_indicator_cache(
    namespace: str, cache_dir: Optional[str] = None
) -> Cache:
    """Enables caching indicator data.

    Args:
        namespace: Namespace of the cache.
        cache_dir: Directory used to store cached indicator data.

    Returns:
        :class:`diskcache.Cache` instance.
    """
    # scope = StaticScope.instance()
    cache_dir = _get_cache_dir(cache_dir, namespace, "indicator")
    # scope.indicator_cache_ns = namespace
    cache = Cache(directory=cache_dir)
    # scope.indicator_cache = cache
    # scope.logger.debug_enable_indicator_cache(namespace, cache_dir)
    return cache


def disable_indicator_cache():
    """Disables caching indicator data."""
    # scope = StaticScope.instance()
    # scope.indicator_cache = None
    # scope.indicator_cache_ns = ""
    # scope.logger.debug_disable_indicator_cache()


def clear_indicator_cache():
    """Clears cached indicator data. :meth:`enable_indicator_cache` must be
    called first before clearing.
    """
    # scope = StaticScope.instance()
    # cache = scope.indicator_cache
    # if cache is None:
    #     raise ValueError(
    #         "Indicator cache needs to be enabled before clearing."
    #     )
    # cache.clear()
    # scope.logger.debug_clear_indicator_cache(cache.directory)


def enable_model_cache(
    namespace: str, cache_dir: Optional[str] = None
) -> Cache:
    """Enables caching trained models.

    Args:
        namespace: Namespace of the cache.
        cache_dir: Directory used to store cached models.

    Returns:
        :class:`diskcache.Cache` instance.
    """
    # scope = StaticScope.instance()
    cache_dir = _get_cache_dir(cache_dir, namespace, "model")
    # scope.model_cache_ns = namespace
    cache = Cache(directory=cache_dir)
    # scope.model_cache = cache
    # scope.logger.debug_enable_model_cache(namespace, cache_dir)
    return cache


def disable_model_cache():
    """Disables caching trained models."""
    # scope = StaticScope.instance()
    # scope.model_cache = None
    # scope.model_cache_ns = ""
    # scope.logger.debug_disable_model_cache()


def clear_model_cache():
    """Clears cached trained models. :meth:`enable_model_cache` must be called
    first before clearing.
    """
    # scope = StaticScope.instance()
    # cache = scope.model_cache
    # if cache is None:
    #     raise ValueError("Model cache needs to be enabled before clearing.")
    # cache.clear()
    # scope.logger.debug_clear_model_cache(cache.directory)


def enable_caches(namespace, cache_dir: Optional[str] = None):
    """Enables all caches.

    Args:
        namespace: Namespace shared by cached data.
        cache_dir: Directory used to store cached data.
    """
    enable_data_source_cache(namespace, cache_dir)
    enable_indicator_cache(namespace, cache_dir)
    enable_model_cache(namespace, cache_dir)


def disable_caches():
    """Disables all caches."""
    disable_data_source_cache()
    disable_indicator_cache()
    disable_model_cache()


def clear_caches():
    """Clears cached data from all caches. :meth:`enable_caches` must be
    called first before clearing."""
    clear_data_source_cache()
    clear_indicator_cache()
    clear_model_cache()
