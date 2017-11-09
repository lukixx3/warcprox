'''
warcprox/dedup.py - identical payload digest deduplication using sqlite db

Copyright (C) 2013-2017 Internet Archive

This program is free software; you can redistribute it and/or
modify it under the terms of the GNU General Public License
as published by the Free Software Foundation; either version 2
of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301,
USA.
'''

from __future__ import absolute_import

from datetime import datetime
import logging
import os
import json
from hanzo import warctools
import warcprox
import sqlite3
import urllib3
from urllib3.exceptions import HTTPError

urllib3.disable_warnings()

class DedupDb(object):
    logger = logging.getLogger("warcprox.dedup.DedupDb")

    def __init__(
            self, file='./warcprox.sqlite', options=warcprox.Options()):
        self.file = file
        self.options = options

    def start(self):
        if os.path.exists(self.file):
            self.logger.info(
                    'opening existing deduplication database %s',
                    self.file)
        else:
            self.logger.info(
                    'creating new deduplication database %s', self.file)

        conn = sqlite3.connect(self.file)
        conn.execute(
                'create table if not exists dedup ('
                '  key varchar(300) primary key,'
                '  value varchar(4000)'
                ');')
        conn.commit()
        conn.close()

    def save(self, digest_key, response_record, bucket=""):
        record_id = response_record.get_header(warctools.WarcRecord.ID).decode('latin1')
        url = response_record.get_header(warctools.WarcRecord.URL).decode('latin1')
        date = response_record.get_header(warctools.WarcRecord.DATE).decode('latin1')

        key = digest_key.decode('utf-8') + "|" + bucket

        py_value = {'id':record_id, 'url':url, 'date':date}
        json_value = json.dumps(py_value, separators=(',',':'))

        conn = sqlite3.connect(self.file)
        conn.execute(
                'insert or replace into dedup (key, value) values (?, ?)',
                (key, json_value))
        conn.commit()
        conn.close()
        self.logger.debug('dedup db saved %s:%s', key, json_value)

    def lookup(self, digest_key, bucket="", url=None):
        result = None
        key = digest_key.decode('utf-8') + '|' + bucket
        conn = sqlite3.connect(self.file)
        cursor = conn.execute('select value from dedup where key = ?', (key,))
        result_tuple = cursor.fetchone()
        conn.close()
        if result_tuple:
            result = json.loads(result_tuple[0])
            result['id'] = result['id'].encode('latin1')
            result['url'] = result['url'].encode('latin1')
            result['date'] = result['date'].encode('latin1')
        self.logger.debug('dedup db lookup of key=%s returning %s', key, result)
        return result

    def notify(self, recorded_url, records):
        if (records and records[0].type == b'response'
                and recorded_url.response_recorder.payload_size() > 0):
            digest_key = warcprox.digest_str(
                    recorded_url.response_recorder.payload_digest,
                    self.options.base32)
            if recorded_url.warcprox_meta and "captures-bucket" in recorded_url.warcprox_meta:
                self.save(
                        digest_key, records[0],
                        bucket=recorded_url.warcprox_meta["captures-bucket"])
            else:
                self.save(digest_key, records[0])


def decorate_with_dedup_info(dedup_db, recorded_url, base32=False):
    if (recorded_url.response_recorder
            and recorded_url.response_recorder.payload_digest
            and recorded_url.response_recorder.payload_size() > 0):
        digest_key = warcprox.digest_str(recorded_url.response_recorder.payload_digest, base32)
        if recorded_url.warcprox_meta and "captures-bucket" in recorded_url.warcprox_meta:
            recorded_url.dedup_info = dedup_db.lookup(digest_key, recorded_url.warcprox_meta["captures-bucket"],
                                                      recorded_url.url)
        else:
            recorded_url.dedup_info = dedup_db.lookup(digest_key,
                                                      url=recorded_url.url)

class RethinkDedupDb:
    logger = logging.getLogger("warcprox.dedup.RethinkDedupDb")

    def __init__(self, rr, table="dedup", shards=None, replicas=None, options=warcprox.Options()):
        self.rr = rr
        self.table = table
        self.shards = shards or len(rr.servers)
        self.replicas = replicas or min(3, len(rr.servers))
        self._ensure_db_table()
        self.options = options

    def _ensure_db_table(self):
        dbs = self.rr.db_list().run()
        if not self.rr.dbname in dbs:
            self.logger.info("creating rethinkdb database %r", self.rr.dbname)
            self.rr.db_create(self.rr.dbname).run()
        tables = self.rr.table_list().run()
        if not self.table in tables:
            self.logger.info(
                    "creating rethinkdb table %r in database %r shards=%r "
                    "replicas=%r", self.table, self.rr.dbname, self.shards,
                    self.replicas)
            self.rr.table_create(
                    self.table, primary_key="key", shards=self.shards,
                    replicas=self.replicas).run()


    def start(self):
        pass

    def save(self, digest_key, response_record, bucket=""):
        k = digest_key.decode("utf-8") if isinstance(digest_key, bytes) else digest_key
        k = "{}|{}".format(k, bucket)
        record_id = response_record.get_header(warctools.WarcRecord.ID).decode('latin1')
        url = response_record.get_header(warctools.WarcRecord.URL).decode('latin1')
        date = response_record.get_header(warctools.WarcRecord.DATE).decode('latin1')
        record = {'key':k,'url':url,'date':date,'id':record_id}
        result = self.rr.table(self.table).insert(
                record, conflict="replace").run()
        if sorted(result.values()) != [0,0,0,0,0,1] and [result["deleted"],result["skipped"],result["errors"]] != [0,0,0]:
            raise Exception("unexpected result %s saving %s", result, record)
        self.logger.debug('dedup db saved %s:%s', k, record)

    def lookup(self, digest_key, bucket="", url=None):
        k = digest_key.decode("utf-8") if isinstance(digest_key, bytes) else digest_key
        k = "{}|{}".format(k, bucket)
        result = self.rr.table(self.table).get(k).run()
        if result:
            for x in result:
                result[x] = result[x].encode("utf-8")
        self.logger.debug('dedup db lookup of key=%s returning %s', k, result)
        return result

    def notify(self, recorded_url, records):
        if (records and records[0].type == b'response'
                and recorded_url.response_recorder.payload_size() > 0):
            digest_key = warcprox.digest_str(recorded_url.response_recorder.payload_digest,
                    self.options.base32)
            if recorded_url.warcprox_meta and "captures-bucket" in recorded_url.warcprox_meta:
                self.save(digest_key, records[0], bucket=recorded_url.warcprox_meta["captures-bucket"])
            else:
                self.save(digest_key, records[0])


class CdxServerDedup(object):
    """Query a CDX server to perform deduplication.
    """
    logger = logging.getLogger("warcprox.dedup.CdxServerDedup")
    http_pool = urllib3.PoolManager()

    def __init__(self, cdx_url="https://web.archive.org/cdx/search",
                 options=warcprox.Options()):
        self.cdx_url = cdx_url
        self.options = options

    def start(self):
        pass

    def save(self, digest_key, response_record, bucket=""):
        """Does not apply to CDX server, as it is obviously read-only.
        """
        pass

    def lookup(self, digest_key, url):
        """Compare `sha1` with SHA1 hash of fetched content (note SHA1 must be
        computed on the original content, after decoding Content-Encoding and
        Transfer-Encoding, if any), if they match, write a revisit record.

        Get only the last item (limit=-1) because Wayback Machine has special
        performance optimisation to handle that. limit < 0 is very inefficient
        in general. Maybe it could be configurable in the future.

        :param digest_key: b'sha1:<KEY-VALUE>' (prefix is optional).
            Example: b'sha1:B2LTWWPUOYAH7UIPQ7ZUPQ4VMBSVC36A'
        :param url: Target URL string
        Result must contain:
        {"url": <URL>, "date": "%Y-%m-%dT%H:%M:%SZ"}
        """
        u = url.decode("utf-8") if isinstance(url, bytes) else url
        try:
            result = self.http_pool.request('GET', self.cdx_url, fields=dict(
                url=u, fl="timestamp,digest", filter="!mimetype:warc/revisit",
                limit=-1))
            assert result.status == 200
            if isinstance(digest_key, bytes):
                dkey = digest_key
            else:
                dkey = digest_key.encode('utf-8')
            dkey = dkey[5:] if dkey.startswith(b'sha1:') else dkey
            line = result.data.strip()
            if line:
                (cdx_ts, cdx_digest) = line.split(b' ')
                if cdx_digest == dkey:
                    dt = datetime.strptime(cdx_ts.decode('ascii'),
                                            '%Y%m%d%H%M%S')
                    date = dt.strftime('%Y-%m-%dT%H:%M:%SZ').encode('utf-8')
                    return dict(url=url, date=date)
        except (HTTPError, AssertionError, ValueError) as exc:
            self.logger.error('CdxServerDedup request failed for url=%s %s',
                              url, exc)
        return None

    def notify(self, recorded_url, records):
        """Since we don't save anything to CDX server, this does not apply.
        """
        pass
