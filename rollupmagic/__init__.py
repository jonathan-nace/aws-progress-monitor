import uuid
import arrow
import logging
import json


class TrackerState(object):
    def __init__(self):
        self.totals = {}
        self.is_done = False
        self.is_failed = False
        self.is_errored = False
        self.is_cancelled = False
        self.is_partially_failed = False
        self.percent_done = 0
        self.start_time = None
        self.finish_time = None

    def start(self, **kwargs):
        self.is_in_progress = True
        self.start_time = kwargs.get('StartTime')
        secs = kwargs.get("EstimatedSeconds", None)
        if secs:
            self.estimated_seconds = int(secs)
        else:
            self.estimated_seconds = None

    def elapsed_time_in_seconds(self):
        if not self.start_time:
            return 0
        return (arrow.utcnow() - self.start_time).total_seconds()

    def remaining_time_in_seconds(self):
        if self.estimated_seconds:
            if self.estimated_seconds > self.elapsed_time_in_seconds:
                return self.estimated_seconds - self.elapsed_time_in_seconds
        return None


class RedisProgressManager(object):
    def __init__(self, **kwargs):
        self.redis = kwargs.get('RedisConnection')
        self.trackers = kwargs.get('Trackers')

    def children_key(self, k):
        return "{}:ch".format(k)

    def update_tracker(self, e):
        pipe = self.redis.pipeline(True)
        data = json.loads(e.to_json())
        pipe.hmset(e.get_full_key(), data)
        if e.children:
            pipe.sadd(self.children_key(e.id), *set(e.children))
        print '---\nsetting {}: {}\n---'.format(e.get_full_key(), data)
        if e.friendly_id:
            pipe.set(e.friendly_id, e.id)
        pipe.execute()

    def get_by_friendly_id(self, friendly_id):
        if (self.redis.exists(friendly_id)):
            id = self.redis.get(friendly_id)
            if id:
                return self.get_all_by_id(id)

    def get_by_id(self, id):
        logging.info('Reading {} from Redis DB'.format(id))
        return self.redis.hgetall(id)

    def get_children(self, id):
        k = self.children_key(id)
        if self.redis.exists(k):
            return self.redis.smembers(self.children_key(id))

    def get_all_by_id(self, id):
        j = self.get_by_id(id)
        if j:
            t = TrackerBase.from_json(id, j)
            t.trackers = self.trackers
            c = self.get_children(id)
            if c:
                t.children = c
            t.db_conn = self
            self.trackers[id] = t

            if len(t.children) > 0:
                for c in t.children:
                    self.get_all_by_id(c)

    def inc_progress(self, e, value=1):
        self.redis.hincrby(e.get_full_key(), "curr_prog", value)


class TrackerBase(object):
    def __init__(self, **kwargs):
        self.friendly_id = kwargs.get('FriendlyId', None)
        self.id = kwargs.get('Id', str(uuid.uuid4()))
        self.name = kwargs.get('Name')
        self.children = []
        self.state = TrackerState()
        self.estimated_seconds = kwargs.get('EstimatedSeconds', None)
        self.parent_id = kwargs.get('ParentId', None)
        self.is_dirty = True
        self.status_msg = None
        self.last_update = arrow.utcnow()
        self.source = kwargs.get('Source', None)
        self.is_in_progress = False
        self.done = False
        self.status = 'Not started'

    def get_tracker_progress_total(self, pe=None):
        t = 0
        c = 0
        if pe is None:
            pe = self
        for k in pe.progress_trackers.keys():
            e = pe.progress_trackers[k]
            t = t + e.progress_total
            c = c + e.current_progress
        return c, t

    def get_progress_remaining(self):
        c, t = self.get_tracker_progress_total()
        return 1-(c/t)

    def get_progress_complete(self):
        c, t = self.get_tracker_progress_total()
        return c/t

    def get_full_key(self):
        if not self.parent_id:
            return self.id
        else:
            return "{}".format(self.id)

    def inc_progress(self, val=1):
        self.db_conn.inc_progress(val)

    def count_children(self, id=None):
        tot = 0
        if id:
            t = self.trackers[id]
        else:
            t = self.trackers[self.id]
            print t.children
        if len(t.children):
            for k in t.children:
                tot = tot + self.count_children(k)
        tot = tot + len(t.children)
        print t.name, tot
        return tot

    def parent(self):
        if self.parent_id in self.trackers.keys():
            return self.trackers[self.parent_id]

    def start(self, **kwargs):
        if self.is_in_progress:
            logging.warning('{} is already started. Ignoring start()'
                            .format(self.id))
            return self
        if self.done:
            logging.warning('{} is done. Ignoring start()')
            return self
        self.is_in_progress = True
        self.start_time = kwargs.get('StartTime', arrow.utcnow())
        if self.parent() and not self.parent().is_in_progress:
            raise Exception("You can't start a tracker if the parent isn't " +
                            'started')

        self.state.start(StartTime=self.start_time,
                         EstimatedSeconds=self.estimated_seconds)
        self.status = 'In Progress'
        self.is_dirty = True
        return self

    def with_status_msg(self, msg):
        self.status_msg = msg
        return self

    def remaining_time_in_seconds(self):
        return self.state.remaining_time_in_seconds()

    def elapsed_time_in_seconds(self):
        return self.state.elapsed_time_in_seconds()

    def update(self):
        if self.parent_id:
            p = self.trackers[self.parent_id]
            p.update()

        if self.is_dirty:
            try:
                self.db_conn.update_tracker(self)
            except Exception:
                logging.error('Error persisting to DB')
                raise
        self.is_dirty = False
        return self

    def to_json(self):
        j = {}
        j['name'] = self.name
        if self.estimated_seconds:
            j['est_sec'] = self.estimated_seconds
        if self.state.start_time:
            j['start'] = self.state.start_time.isoformat()
        if self.state.finish_time:
            j['finish'] = self.state.finish_time.isoformat()
        if self.status_msg:
            j['st_msg'] = self.status_msg
        if self.parent_id:
            j['pid'] = self.parent_id
        if self.friendly_id:
            j['fid'] = self.friendly_id
        if self.last_update:
            j['l_u'] = self.last_update.isoformat()
        if self.source:
            j['s'] = self.source
        j['in_p'] = self.is_in_progress
        j['d'] = self.done
        if self.status:
            j['st'] = self.status
        return json.dumps(j)

    @staticmethod
    def from_json(id, j):
        t = ProgressTracker(Id=id)
        if 'name' in j.keys():
            t.with_name(j['name'])
        if 'est_sec' in j.keys():
            t.with_estimated_seconds(j['est_sec'])
        if 'start' in j.keys():
            t.with_start_time(arrow.get(j['start']))
        if 'in_p' in j.keys():
            t.is_in_progress = j['in_p'] == 'True'
        if 'st' in j.keys():
            t.status = j['st']
        if 'pid' in j.keys():
            t.parent_id = j['pid']
        if 's' in j.keys():
            t.source = j['s']
        if 'd' in j.keys():
            t.done = j['d'] == 'True'
        t.is_dirty = False
        return t

    def with_child(self, c):
        if self.children and c in self.children:
            return self
        self.is_dirty = True
        self.children.append(c)
        return self

    def with_children(self, c):
        self.is_dirty = not self.children == c
        self.children = c
        return self

    def with_estimated_seconds(self, s):
        self.is_dirty = not self.estimated_seconds == s
        self.estimated_seconds = s
        return self

    def with_last_update(self, d):
        self.is_dirty = not self.last_update == d
        self.last_update = d
        return self

    def with_source(self, s):
        self.is_dirty = not self.source == s
        self.source = s
        return self

    def with_start_time(self, s):
        self.is_dirty = not self.start_time == s
        self.start_time = s
        self.state.start_time = s
        return self

    def with_finish_time(self, f):
        self.is_dirty = not self.finish_time == f
        self.finish_time = f
        self.state.start_time = f
        return self

    def with_metric(self, **kwargs):
        self.metric_namespace = kwargs.get('Namespace')
        self.metric = kwargs.get('Metric')
        return self


class ProgressTracker(TrackerBase):
    def __init__(self, **kwargs):
        self.start_time = kwargs.get('StartTime', arrow.utcnow().isoformat())
        self.done = False
        super(ProgressTracker, self).__init__(**kwargs)

    def with_name(self, n):
        if not self.name == n:
            self.name = n
            self.dirty = True
        return self

    def with_message(self, m):
        if not self.message == m:
            self.message = m
            self.dirty = True
        return self

    def with_timestamp(self, m):
        if not m:
            m = arrow.utcnow().isoformat()
        self.dirty = True
        return self

    def succeed(self):
        if self.status == 'Succeeeded' and self.done and \
                          not self.is_in_progress:
            return self
        self.status = 'Succeeded'
        self.done = True
        self.is_in_progress = False
        self.is_dirty = True
        return self

    def cancel(self):
        if self.status == 'Canceled' and self.done and not self.is_in_progress:
            return self
        self.status = 'Canceled'
        self.is_in_progress = False
        self.done = True
        self.is_dirty = True
        return self

    def fail(self):
        if self.status == 'Failed' and self.done and not self.is_in_progress:
            return self
        self.status = 'Failed'
        self.done = True
        self.is_in_progress = False
        self.is_dirty = True
        return self


class ProgressMagician(TrackerBase):
    def __init__(self, **kwargs):
        self.db_conn = kwargs.get('DbConnection')
        self.name = kwargs.get('Name')
        self.trackers = {}
        super(ProgressMagician, self).__init__(**kwargs)
        self.trackers[self.id] = self
        self.db_conn.trackers = self.trackers

    def load(self, id):
        self.id = id
        self.db_conn.get_all_by_id(id)
        print self.trackers
        self = self.trackers[id]
        # self.progress_trackers = {}
        # for i in all_items.keys():
        #    self.add_progress_tracker(i, all_items[i])

    def with_tracker(self, t):
        t.db_conn = self.db_conn
        if not t.parent_id:
            t.parent_id = self.id
        t.trackers = self.trackers
        p = self.trackers[t.parent_id]
        if t.id not in p.children:
            p.with_child(t.id)
        self.trackers[t.id] = t
        return self

    def find_friendly_id(self, fk):
        for k in self.trackers.keys():
            if self.trackers[k].friendly_id == fk:
                return self.trackers[k]
        return None

    def find_id(self, id):
        if id in self.trackers.keys():
            return self.trackers[id]
        return None

    def update_all(self):
        for i in self.trackers.keys():
            t = self.trackers[i]
            if self.trackers[i].is_dirty:
                t.update()
        if self.is_dirty:
            self.update()
        return self
