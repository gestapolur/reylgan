# -*- coding: utf-8 -*-
"""
this module controll db insertion
"""
from __future__ import absolute_import, division, print_function, unicode_literals

import threading
import time
import logging
import re
import sys

import pymongo
import redis
import arrow
from pymongo.errors import DuplicateKeyError

from tweets import Tweets
from config import *

if sys.version_info.major > 2:
    text_type = str
else:
    text_type = unicode


class RedisQueueSet(object):

    def __init__(self):
        self.redis_conn = redis.from_url(env.redis_url)

    def put(self, s):
        return self.redis_conn.zadd(REDIS_QUEUE, s, time.time())

    def pop(self):
        pipe = self.redis_conn.pipeline(transaction=True)
        pipe.zrange(REDIS_QUEUE, 0, 0)
        pipe.zremrangebyrank(REDIS_QUEUE, 0, 0)
        return int(pipe.execute()[0][0])

    def size(self):
        return self.redis_conn.zcard(REDIS_QUEUE)


class Worker(threading.Thread):
    def __init__(self):
        super(Worker, self).__init__()
        self.db = pymongo.MongoClient(env.mongodb_url)["reylgan"]
        self.daemon = True
        self.queue = RedisQueueSet()

    def _push_to_db(self, data, collect):
        """
        @todo upsert each user info
        """
        logging.info(
            "pushing %d items to collection %s." % (len(data), collect))
        try:
            assert len(data)
            bulk = self.db[collect].initialize_ordered_bulk_op()
            for item in data:
                bulk.find(
                    {"id": item["id"]}).upsert().update(
                    {"$set": item})
            bulk.execute()
        except AssertionError as err:
            logging.warning("data is empty")

    def run(self):
        tweets = Tweets()
        while True:
            if not self.queue.size():
                logging.warning("queue is empty")
                time.sleep(CRAWLER_COLDDOWN_TIME)
                continue
            user_id = self.queue.pop()
            logging.info("fetching user %s." % user_id)
            """
            print (self.name, user_id)
            pull tweet, user's follower & friends
            push em to db.
            """
            self._push_to_db(tweets.get_user_timeline(user_id, count=50),
                             "tweets")
            self._push_to_db(tweets.get_user_list(user_id), "users")
            self._push_to_db(tweets.get_user_list(user_id,
                                                  url=TWITTER_FRIENDS_LIST),
                             "users")

            time.sleep(CRAWLER_COLDDOWN_TIME)
            self.queue.put(user_id)


HTTP_REGEX_STR = '(https?://[\S]+)'
PUNCT_STR = ("([!@#\$%\^\&\*\(\)_\+\-=<>\/\:\"\';\|\\~!;,\.\?"
             "「」～！…（）；：，。？． ]+)")
REPLACE_IRRELEVANT_REGEX = re.compile("|".join([HTTP_REGEX_STR,
                                                PUNCT_STR,
                                                '([0-9]+)']))

class Analyzer(threading.Thread):
    def __init__(self):
        super(Analyzer, self).__init__()
        self.db = pymongo.MongoClient(env.mongodb_url)["reylgan"]
        self.daemon = True
        self.queue = RedisQueueSet()

    @staticmethod
    def detect_chinese(tweets, rate=0.5):
        """
        @todo: use a classifier model instead
        """
        def detect(text):
            s = REPLACE_IRRELEVANT_REGEX.sub(
                "",
                (text if isinstance(text, text_type)
                 else text.decode('utf-8')))
            # normal chinese character range [19968, 40908]
            cnt = sum([1 for _ in s if 19968 <= ord(_) <= 40908])
            return cnt/float(len(text))

        ans = 0
        assert len(tweets)
        for tweet in iter(tweets):
            # zh-cn/zh-tw
            if "zh" in tweet.get('lang', ''): ans += 1.0
            # fix twitter's own language detect
            elif tweet.get('lang', '') == 'ja': ans += detect(tweet['text'])
        ans /= float(len(tweets))
        logging.debug("chinese user confidence: %s" % ans)
        return True if ans >= rate else False

    @staticmethod
    def compute_average_tweets(tweets, rate=1.0, time_window=14):
        current_time = time.time()
        cnt = 0
        for tweet in iter(tweets):
            created_at = arrow.Arrow.strptime(
                tweet["created_at"],
                "%a %b %d %H:%M:%S %z %Y").timestamp
            if created_at >= current_time - time_window*24*3600:
                cnt += 1
        logging.debug("user %s active rate %s" % (
                tweets[0]["user"]["id"], cnt/float(time_window)))
        return True if cnt/float(time_window) >= rate else False

    def find_active_zh_user(self, tweets):
        """
        average tweets > 1 in a month 
        """
        cursor = self.db["users"].find(
            {"is_zh_user": True}).sort("followers_count",
                                       pymongo.DESCENDING)
        for user in cursor:
            recent_tweets = self.db["tweets"].find(
                {"id": user["id"]}).limit(100)

            if recent_tweets.count() < 50:
                recent_tweets = tweets.get_user_timeline(
                    user["id"], count=100, max_collect=100)
            else:
                logging.debug(
                    "fetch recent %s tweets of user %s" % (
                        recent_tweets.count(),
                        user["id"]))
                recent_tweets = [_ for _ in recent_tweets]

            if len(recent_tweets) > 14 and \
                    self.compute_average_tweets(recent_tweets):
                logging.debug("user %s is a active user." % user["id"])
                self.db["users"].update(
                    {"id": user["id"]},
                    {"$set": {"is_active_user": True}})


    def find_new_zh_user(self, tweets):
        logging.info("%s user_id in queue" % self.queue.size())
        cursor = self.db["users"].find(
            {"is_zh_user": {"$ne": False}}).sort("followers_count",
                                                 pymongo.DESCENDING)
        for user in cursor:
            if "is_zh_user" in user:
                if self.queue.put(user["id"]):
                    logging.info(
                        "put new user %s to queue." % user["id"])
                else:
                    logging.debug(
                        "user %s already in queue." % user["id"])
            else:
                recent_tweets = self.db["tweets"].find(
                    {"id": user["id"]}).limit(100)

                if recent_tweets.count() < 50:
                    recent_tweets = tweets.get_user_timeline(
                        user["id"], count=100, max_collect=100)
                else:
                    logging.debug(
                        "fetch recent %s tweets of user %s" % (
                            recent_tweets.count(),
                            user["id"]))
                    recent_tweets = [_ for _ in recent_tweets]

                result = True if len(recent_tweets) >= 50 \
                    and self.detect_chinese(recent_tweets) \
                    else False
                logging.debug(
                    "user %s detect result: %s" % (user["id"],
                                                   result))
                self.db["users"].update(
                    {"id": user["id"]},
                    {"$set": {"is_zh_user": result}})


    def run(self):
        logging.info("analyzer started")
        tweets = Tweets()
        while True:
            self.find_new_zh_user(tweets)
            self.find_active_zh_user(tweets)
            logging.info("sleep a while")
            time.sleep(30)
