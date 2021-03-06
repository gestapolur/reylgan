# -*- coding: utf-8 -*-
"""
this module provide tweet fetching functions
"""

from __future__ import absolute_import, division, print_function, unicode_literals

import base64
import time
import logging

import requests

from config import *


class Tweets(object):

    def __init__(self):
        self.session = requests.session()
        self.access_token = None

    def _handle_crawl_error(self):
        raise NotImplementedError

    def _obtain_access_token(self):
        """
        @todo token may get expired
        """
        encoded_bearer = base64.b64encode(('%s:%s' % (
                env.client_key, env.client_secret)).encode("utf-8"))
        headers = {"User-Agent": env.user_agent,
                   "Authorization": "Basic %s" % encoded_bearer.decode(
                "utf-8"),
                   "Content-Type": "application/x-www-form-urlencoded;"
                   "charset=UTF-8",
                   "Accept-Encoding": "gzip"}
        res = self.session.post(TWITTER_OAUTH_URL,
                           headers=headers,
                           data="grant_type=client_credentials")
        assert res.status_code == 200
        return res.json()["access_token"]

    def ensure_access_token(f):
        def wrapper(self, *args, **kwargs):
            if not self.access_token:
                self.access_token = self._obtain_access_token()
            return f(self, *args, **kwargs)
        return wrapper

    @ensure_access_token
    def get_user_timeline(self, user_id, count=50, max_collect=500):
        """
        https://dev.twitter.com/rest/reference/get/statuses/user_timeline
        """
        user_tweets = []
        max_id_str = ""
        while True:
            res = self.session.get(
                "%s?user_id=%s%s&count=%s&include_rts=1" % (
                    TWITTER_USER_TIMELINE,
                    user_id,
                    max_id_str,
                    count),
                headers={"Authorization": "Bearer %s" % self.access_token}
            )
            if res.status_code != 200:
                print (res.json)
                if res.status_code == 401:  # key not valid or locked user
                    logging.error(res.json()["error"])
                    break
                else:  # rate limited exceeded or Twitter dead
                    logging.error(res.json()["errors"])
                    logging.info("waiting 15 min...")
                time.sleep(900)
            else:
                res = res.json()
                if len(res):
                    user_tweets.extend(res)
                    # print ([it["text"] for it in res])
                    if len(user_tweets) >= max_collect:
                        break
                    else:
                        max_id_str = "&max_id=%s" % (user_tweets[-1]["id"]-1)
                else:
                    logging.warning(
                        "no tweets found on user %s, stop" % user_id)
                    break

        logging.info("fetch %s tweets from user %s" % (len(user_tweets),
                                                       user_id))
        return user_tweets

    @ensure_access_token
    def get_user_list(self, user_id, url=TWITTER_FOLLOWER_LIST):
        """
        return a list of user
        @todo for user who has over to much followers, maybe we should only
        fetch part of them.
        """
        user_list = []
        next_cursor = -1
        while True:
            res = self.session.get(
                "%s?user_id=%s&cursor=%s" % (
                    url,
                    user_id,
                    next_cursor),
                headers={"Authorization": "Bearer %s" % self.access_token}
                ).json()

            if "errors" in res:
                # rate limited exceeded or Twitter dead
                logging.error(res["errors"])
                logging.info("waiting 15 min...")
                time.sleep(900)
            else:
                user_list.extend(res["users"])
                logging.debug(
                    "fetched user: " +
                    " ".join([_["name"] for _ in res["users"]]))
                # @todo set a proper value
                if len(user_list) > 1000:
                    break
                if res["next_cursor"] <= 0:
                    break
                else:
                    next_cursor = res["next_cursor"]
        logging.info("fetch %s followers from user %s" % (len(user_list),
                                                          user_id))
        return user_list


if __name__ == "__main__":
    tweets = Tweets()
    print (tweets.get_user_timeline(user_id=115763683, count=1))
