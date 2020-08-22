# ©2015-20 Graham Eddy <graham.eddy@gmail.com>
# Distributed under the terms of the GNU Public License (GPLv3)
"""
sedge module provides weewx data service for SolarEdge inverter readings

SolarEdgeSvc: class for weewx service acquiring SolarEdge energy data
SolarEdge: SolarEdge inverter-facing class
"""

import sys
if sys.version_info[0] < 3:
    raise ImportError("requires python3")
import queue
import logging
import requests
import threading
import time

import weewx
import weewx.units
from weewx.engine import StdService

log = logging.getLogger(__name__)
version = "2.2"


class SolarEdgeSvc(StdService):
    """
    service that gets solar energy readings from SolarEdge cloud

    pseudo-driver for solaredge inverter data uploaded into cloud by vendor
    and inserts its values into next LOOP record

    weewx.conf configuration:
    [SolarEdge]
        api_key     your api key from_vendor (no default)
        site_id     your site id from installer (no default)
        data_type   data_type to be inserted (default: solarEnergy)
        poll_interval polling interval /secs (default: 15 mins, must be at
                    least this long due to api limitations)
        binding     one or more of 'loop', 'archive' (default: archive)

    example weewx.conf entry:
    [SolarEdge]
        api_key = your_api_key_from_vendor
        site_id = your_site_id_from_installer
        #data_type = solarEnergy
        #poll_interval = 900
        #binding = archive
    """

    DEF_DATA_TYPE = 'solarEnergy'
    DEF_POLL_SECS = 900
    DEF_BINDING = 'archive'

    UNIT = 'watt_hour'
    UNIT_GROUP = 'group_energy'

    def __init__(self, engine, config_dict):
        super(SolarEdgeSvc, self).__init__(engine, config_dict)

        log.debug(f"{self.__class__.__name__}: starting (version {version})")

        # configuration
        try:
            svc_sect = config_dict['SolarEdge']
            api_key = svc_sect['api_key']
            site_id = svc_sect['site_id']
            self.data_type = svc_sect.get(
                                'data_type', SolarEdgeSvc.DEF_DATA_TYPE)
            poll_interval = int(svc_sect.get(
                                'poll_interval', SolarEdgeSvc.DEF_POLL_SECS))
            binding = svc_sect.get('binding', SolarEdgeSvc.DEF_BINDING)
        except KeyError as e:
            log.error(f"{self.__class__.__name__}: config missing: {e.args[0]}")
            return      # slip away without becoming a packet listener
        except ValueError as e:
            log.error(f"{self.__class__.__name__}: invalid config: {e.args[0]}")
        if poll_interval < SolarEdge.TIME_UNIT_SECS:
            log.error(f"{self.__class__.__name__}:"
                      f" poll_interval={poll_interval}"
                      f" must be at least {SolarEdge.TIME_UNIT_SECS} secs")
            return      # slip away without becoming a packet listener
        if 'loop' not in binding and 'archive' not in binding:
            log.error(f"{self.__class__.__name__}: binding={binding}"
                      f" must include 'loop' or 'archive'")
            return      # slip away without becoming a packet listener

        # create 'stop' signal for threads
        self.stop = threading.Event()

        # create queue between engine and service threads
        self.q = queue.Queue()

        # create device interface
        device = SolarEdge(api_key, site_id)

        # spawn acquirer
        try:
            self.acquirer = threading.Thread(target=device.run,
                                    args=(poll_interval, self.q, self.stop))
            self.acquirer.start()
        except threading.ThreadError as e:
            log.error(f"{self.__class__.__name__}: thread failed: {repr(e)}")
            return      # slip away without becoming a packet listener

        # start listening to new packets
        if 'loop' in binding:
            self.bind(weewx.NEW_LOOP_PACKET, self.new_loop_record)
            if weewx.debug > 1:
                log.debug(f"{self.__class__.__name__}: bound to 'loop'")
        if 'archive' in binding:
            self.bind(weewx.NEW_ARCHIVE_RECORD, self.new_archive_record)
            if weewx.debug > 1:
                log.debug(f"{self.__class__.__name__}: bound to 'archive'")
        log.info(f"{self.__class__.__name__} started (version {version})")

    def new_loop_record(self, event):
        self.update(event.packet)

    def new_archive_record(self, event):
        self.update(event.record)

    def update(self, packet):
        """handle packet by inserting queued readings"""

        readings_count = 0
        try:
            while not self.q.empty():
                raw_value = self.q.get(block=False)

                # convert to packet's unit_system
                raw_vt = (raw_value, SolarEdgeSvc.UNIT, SolarEdgeSvc.UNIT_GROUP)
                pkt_vt = weewx.units.convertStd(raw_vt, packet['usUnits'])

                # insert value into packet.
                # overwrite previous if several in queue
                if weewx.debug > 0:
                    log.debug(f"{self.__class__.__name__}.update"
                              f" packet[{self.data_type}]={pkt_vt[0]}")
                packet[self.data_type] = pkt_vt[0]

                self.q.task_done()
                readings_count += 1
        except queue.Empty:
            if weewx.debug > 1:
                log.debug(f"{self.__class__.__name__}.update: queue.Empty")
            pass        # corner case that can be ignored
        if readings_count > 0:
            log.info(f"{self.__class__.__name__}: {readings_count} readings"
                     f" found")
        elif weewx.debug > 1:
            log.debug(f"{self.__class__.__name__}.update: no readings found")

    def shutDown(self):
        """respond to request for graceful shutdown"""

        log.info(f"{self.__class__.__name__}: shutdown")
        self.stop.set()             # request all threads to stop
        self.acquirer.kill()        # poke if sleeping


class SolarEdge:
    """class facing SolarEdge inverter"""

    API_URL = 'http://monitoringapi.solaredge.com'
    TIME_UNIT_SECS = 900
    TIME_UNIT_LABEL = 'QUARTER_OF_AN_HOUR'

    def __init__(self, api_key, site_id):

        self.api_key = api_key
        self.site_id = site_id

        self.timer = None

    def run(self, poll_interval, q, stop):
        """insert readings from SolarEdge cloud onto queue"""

        if weewx.debug > 1:
            log.debug(f"{self.__class__.__name__}.run start")

        next_time = time.time()     # time scheduled for next update; starts now
        last_time = next_time - poll_interval
                                    # time of last success
        while not stop.is_set():

            # fetch energy readings for interval since last success
            try:
                pkg = self.get_energy_details(last_time, next_time)
            except requests.exceptions.RequestException as e:
                log.error(f"{self.__class__.__name__}: {repr(e)}")
            else:
                total = self.extract_readings(pkg, last_time)
                if total is not None:
                    # successfully found a reading
                    if weewx.debug > 0:
                        log.debug(f"{self.__class__.__name__}.run:"
                                  f" put total={total}")
                    q.put(total)
                    last_time = next_time   # narrow the next interval
                else:
                    if weewx.debug > 0:
                        log.debug(f"{self.__class__.__name__}.run: no total")

            # schedule next reading
            while True:
                next_time += poll_interval
                if next_time > time.time() + 5.0:
                    break       # min 5 secs sleep!
            if weewx.debug > 0:
                log.debug(f"{self.__class__.__name__}.run: start long sleep"
                          f" {next_time - time.time()} secs")
            self.delay(next_time - time.time())

        # we have been requested to stop
        if weewx.debug > 1:
            log.debug(f"{self.__class__.__name__} finish")

    def get_energy_details(self, start_time, end_time):
        """retrieve energy readings for specified time interval"""

        if weewx.debug > 1:
            log.debug(f"{self.__class__.__name__}.get_energy_details"
                      f" start_time={start_time} end_time={end_time}")

        response = requests.get(
            f'{SolarEdge.API_URL}/site/{self.site_id}/energyDetails',
            params={
                'startTime': SolarEdge.epoch2date(start_time),
                'endTime': SolarEdge.epoch2date(end_time),
                'timeUnit': SolarEdge.TIME_UNIT_LABEL,
                'api_key': self.api_key
            })
        response.raise_for_status()

        readings = response.json()
        if weewx.debug > 1:
            log.debug(f"{self.__class__.__name__}.get_energy_details"
                      f" readings={readings}")
        return readings

    def extract_readings(self, pkg, start_time):
        """extract a total from readings, in watt_hours or None"""

        try:
            # sum the energy quantities in the interval since last success
            readings = pkg['energyDetails']['meters'][0]['values']
            if weewx.debug > 1:
                log.debug(f"{self.__class__.__name__}.extract_readings:"
                          f" len(readings)={len(readings)}")
            count_ok_readings = 0
            total = 0.0
            for reading in readings:
                if 'value' not in reading:
                    # probably implicit zero, not considered a valid reading
                    if weewx.debug > 1:
                        log.debug(f"{self.__class__.__name__}.extract_readings"
                                  f": absent value reading")
                    continue
                epoch = SolarEdge.date2epoch(reading['date'])
                if epoch < start_time:
                    # api often inserts leading readings from earlier than
                    # requested start
                    if weewx.debug > 1:
                        log.debug(f"{self.__class__.__name__}.extract_readings"
                                  f": skipped too-early reading")
                    continue
                # we have a current explicit reading (which might be zero)
                count_ok_readings += 1
                if weewx.debug > 1:
                    log.debug(f"{self.__class__.__name__}.extract_readings:"
                              f" value={reading['value']}"
                              f" time={reading['date']}")
                total += float(reading['value'])
            if count_ok_readings <= 0:
                if weewx.debug > 1:
                    log.debug(f"{self.__class__.__name__}.extract_readings:"
                              f" no useful readings -> skipped")
                return None

            # scale to watt-hours
            scale = 1 if pkg['energyDetails']['unit'] == 'Wh' else 1000
            total *= scale

        except KeyError as e:
            log.warning(f"{self.__class__.__name__}: reading missing key:"
                        f" {e.args[0]}")
            return None
        except ValueError as e:
            log.warning(f"{self.__class__.__name__}: reading: {e.args[0]}")
            return None

        return total

    def delay(self, secs):
        """delay for a while using timer, or sleep if timer fails"""

        def nothing(): pass

        try:
            self.timer = threading.Timer(secs, nothing)
            self.timer.start()
            self.timer.join()  # cancellable :-)
            self.timer = None
        except threading.ThreadError as e:
            if weewx.debug > 0:
                log.debug(f"{self.__class__.__name__}.delay: timer failed:"
                          f" {repr(e)}, using sleep instead")
            time.sleep(secs)

    @staticmethod
    def epoch2date(epoch):
        """convert UNIX epoch time to SolarEdge date format"""
        return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(epoch))

    @staticmethod
    def date2epoch(date):
        """convert SolarEdge date format to UNIX epoch time"""
        return time.mktime(time.strptime(date, '%Y-%m-%d %H:%M:%S'))

    def kill(self):
        """make best attempt to terminate thread"""

        if self.timer:
            self.timer.cancel()

