import time

import yagi.auth
import yagi.config
import yagi.handler
import yagi.log
import yagi.serializer.atom
from yagi import stats
from yagi import http_util

with yagi.config.defaults_for("atompub") as default:
    default("validate_ssl", "False")
    default("retries", "-1")
    default("url", "http://127.0.0.1/nova")
    default("max_wait", "600")
    default("failures_before_reauth", "5")
    default("interval", "30")

LOG = yagi.log.logger


class MessageDeliveryFailed(Exception):
    def __init__(self, msg, code, *args):
        self.code = code
        self.msg = msg
        args = (msg, code) + args
        super(MessageDeliveryFailed, self).__init__(*args)


class UnauthorizedException(Exception):
    pass


class AtomPub(yagi.handler.BaseHandler):
    CONFIG_SECTION = "atompub"
    AUTO_ACK = True

    def _send_notification(self, endpoint, puburl, headers, body, conn):
        LOG.debug("Sending message to %s with body: %s" % (endpoint, body))
        headers["Content-Type"] = "application/atom+xml"
        try:
            resp, content = conn.request(endpoint, "POST",
                                         body=body,
                                         headers=headers)
            if resp.status == 401:
                raise UnauthorizedException()
            if resp.status != 201:
                msg = ("AtomPub resource create failed for %s Status: "
                            "%s, %s" % (puburl, resp.status, content))
                raise MessageDeliveryFailed(msg, resp.status)
            return resp.status
        except http_util.ResponseTooLargeError, e:
            if e.response.status == 201:
                # Was successfully created. Reply was just too large.
                # Note that we DON'T want to retry this if we've gotten a 201.
                LOG.error("Response too large on successful post")
                LOG.exception(e)
            else:
                msg = ("AtomPub resource create failed for %s. "
                       "Also, response was too large." % puburl )
                raise MessageDeliveryFailed(msg, e.response.status)
        except MessageDeliveryFailed, e:
            #catch and reraise to prevent Exception block from catching these.
            raise
        except Exception, e:
            msg = ("AtomPub Delivery Failed to %s with:\n%s" % (endpoint, e))
            raise MessageDeliveryFailed(msg, 0)

    def new_http_connection(self, force=False):
        ssl_check = not (self.config_get("validate_ssl") == "True")
        conn = http_util.LimitingBodyHttp(
                        disable_ssl_certificate_validation=ssl_check)
        auth_method = yagi.auth.get_auth_method()
        headers = {}
        if auth_method:
            try:
                auth_method(conn, headers, force=force)
            except Exception, e:
                # Auth could be jacked for some reason, slow down on failing.
                # Alternatively, if we have bad credentials, don't fill
                # up the logs crying about it.
                LOG.exception(e)
                interval = int(self.config_get("interval"))
                time.sleep(interval)
        else:
            raise Exception("Invalid auth or no auth supplied")
        return conn, headers

    def handle_messages(self, messages, env):
        retries = int(self.config_get("retries"))
        interval = int(self.config_get("interval"))
        max_wait = int(self.config_get("max_wait"))
        failures_before_reauth = int(self.config_get("failures_before_reauth"))
        conn, headers = self.new_http_connection()
        results = env.setdefault('atompub.results', dict())

        for payload in self.iterate_payloads(messages, env):
            msgid = payload["message_id"]
            code = 0
            error = False
            message = ''
            try:
                entity = dict(content=payload,
                              id=payload["message_id"],
                              event_type=payload["event_type"])
                payload_body = yagi.serializer.atom.dump_item(entity)
            except KeyError, e:
                error_msg = "Malformed Notification: %s" % payload
                LOG.error(error_msg)
                LOG.exception(e)
                results[msgid] = dict(error=True, code=0, message=error_msg)
                continue

            endpoint = self.config_get("url")
            tries = 0
            failures = 0
            while True:
                try:
                    code = self._send_notification(endpoint, endpoint,
                                                   headers,
                                                   payload_body,
                                                   conn)
                    error = False
                    msg = ''
                    break
                except MessageDeliveryFailed, e:
                    stats.increment_stat(yagi.stats.failure_message())
                    LOG.exception(e)

                    # Number of overall tries
                    tries += 1

                    # Number of tries between re-auth attempts
                    failures += 1

                    # Used primarily for testing, but it's possible we don't
                    # care if we lose messages?
                    if retries > 0:
                        tries += 1
                        if tries >= retries:
                            error_msg = "Exceeded retry limit. Error %s" % e.msg
                            results[msgid] = dict(error=False, code=e.code, message=error_msg)
                            break
                    wait = min(tries * interval, max_wait)
                    LOG.error("Message delivery failed, going to sleep, will "
                             "try again in %s seconds" % str(wait))
                    time.sleep(wait)

                    if failures >= failures_before_reauth:
                        # Don't always try to reconnect, give it a few
                        # tries first
                        failures = 0
                        conn, headers = self.new_http_connection(force=True)
                except UnauthorizedException:
                    LOG.exception(e)
                    conn, headers = self.new_http_connection(force=True)

            results[msgid] = dict(error=False, code=code, message="Success")


