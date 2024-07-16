# Copyright (C) 2020-2023 Matt Baker <baker.matt.j@gmail.com>
# Copyright (C) 2020-2023 Lasse Dalegaard <dalegaard@gmail.com>
# Copyright (C) 2023 Beacon <beacon3d.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import threading
import multiprocessing
import importlib
import traceback
import logging
import chelper
import pins
import math
import time
import queue
import json
import struct
import numpy as np
import copy
from numpy.polynomial import Polynomial
from . import manual_probe
from . import probe
from . import bed_mesh
from . import thermistor
from . import adc_temperature
from . import manual_probe
from mcu import MCU, MCU_trsync
from clocksync import SecondarySync

STREAM_BUFFER_LIMIT_DEFAULT = 100
STREAM_TIMEOUT = 2.0

class IDMProbe:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name()

        self.speed = config.getfloat("speed", 5.0, above=0.0)
        self.lift_speed = config.getfloat("lift_speed", self.speed, above=0.0)
        self.backlash_comp = config.getfloat("backlash_comp", 0.5)

        self.x_offset = config.getfloat("x_offset", 0.0)
        self.y_offset = config.getfloat("y_offset", 0.0)
        self.z_offset = config.getfloat("z_offset", 0.0)
        self.probe_calibrate_z = 0.

        self.probe_speed = config.getfloat("probe_speed", 5.0)
        self.tap_location = config.get("tap_location").split(",")
        self.calibration_method = config.get("calibration_method","scan")
        self.trigger_method = 0

        self.trigger_distance = config.getfloat("trigger_distance", 2.0)
        self.trigger_dive_threshold = config.getfloat("trigger_dive_threshold", 1.0)
        self.trigger_hysteresis = config.getfloat("trigger_hysteresis", 0.006)
        self.z_settling_time = config.getint("z_settling_time", 5, minval=0)

        # If using paper for calibration, this would be .1mm
        self.cal_nozzle_z = config.getfloat("cal_nozzle_z", 0.1)
        self.cal_floor = config.getfloat("cal_floor", 0.2)
        self.cal_ceil = config.getfloat("cal_ceil", 5.0)
        self.cal_speed = config.getfloat("cal_speed", 1.0)
        self.cal_move_speed = config.getfloat("cal_move_speed", 10.0)

        # Load models
        self.model = None
        self.models = {}
        self.model_temp_builder = IDMTempModelBuilder.load(config)
        self.model_temp = None
        self.fmin = None
        self.default_model_name = config.get("default_model_name", "default")
        self.model_manager = ModelManager(self)

        # Temperature sensor integration
        self.last_temp = 0
        self.measured_min = 99999999.0
        self.measured_max = 0.0

        self.last_sample = None
        self.hardware_failure = None

        self.mesh_helper = IDMMeshHelper.create(self, config)

        self._stream_en = 0
        self._stream_timeout_timer = self.reactor.register_timer(self._stream_timeout)
        self._stream_callbacks = {}
        self._stream_latency_requests = {}
        self._stream_buffer = []
        self._stream_buffer_limit = STREAM_BUFFER_LIMIT_DEFAULT
        self._stream_buffer_limit_new = self._stream_buffer_limit
        self._stream_samples_queue = queue.Queue()
        self._stream_flush_event = threading.Event()
        self._log_stream = None
        self._data_filter = AlphaBetaFilter(
            config.getfloat("filter_alpha", 0.5),
            config.getfloat("filter_beta", 0.000001),
        )
        self.trapq = None
        self._last_trapq_move = None
        self.mod_axis_twist_comp = None
        self.raw_axis_twist_comp = None

        mainsync = self.printer.lookup_object("mcu")._clocksync
        mcu = config.get("mcu",None)
        if not mcu is None:
            if mcu == "mcu":
                self._mcu = self.printer.lookup_object("mcu")
            else:
                self._mcu = self.printer.lookup_object("mcu " + mcu)
        else:
            self._mcu = MCU(config, SecondarySync(self.reactor, mainsync))
            self.printer.add_object("mcu " + self.name, self._mcu)
        self.cmd_queue = self._mcu.alloc_command_queue()
        self.mcu_probe = IDMEndstopWrapper(self)

        ppins = self.printer.lookup_object('pins')
        probe_pin = config.get('probe_pin',"none")
        if probe_pin != "none":
            pin_params = ppins.lookup_pin(probe_pin, can_invert=True, can_pullup=True)
            endstop_mcu = pin_params['chip']
            self.endstop_mcu_endstop = endstop_mcu.setup_pin('endstop', pin_params)
            self.endstop_add_stepper = self.endstop_mcu_endstop.add_stepper
        else:
            self.endstop_mcu_endstop = None
            self.endstop_add_stepper  = None
        # Register z_virtual_endstop
        self.printer.lookup_object("pins").register_chip("probe", self)
        # Register event handlers
        self.printer.register_event_handler("klippy:connect",
                                            self._handle_connect)
        self.printer.register_event_handler("klippy:mcu_identify",
                                            self._handle_mcu_identify)
        self._mcu.register_config_callback(self._build_config)
        self._mcu.register_response(self._handle_idm_data, "idm_data")
        # Probe results
        self.results = []
        # Register webhooks
        webhooks = self.printer.lookup_object("webhooks")
        self._api_dump_helper = APIDumpHelper(self)
        webhooks.register_endpoint("idm/status", self._handle_req_status)
        webhooks.register_endpoint("idm/dump", self._handle_req_dump)
        # Register gcode commands
        self.gcode = self.printer.lookup_object("gcode")
        self.gcode.register_command("IDM_STREAM", self.cmd_IDM_STREAM,
                                    desc=self.cmd_IDM_STREAM_help)
        self.gcode.register_command("IDM_QUERY", self.cmd_IDM_QUERY,
                                    desc=self.cmd_IDM_QUERY_help)
        self.gcode.register_command("IDM_CALIBRATE",
                                    self.cmd_IDM_CALIBRATE,
                                    desc=self.cmd_IDM_CALIBRATE_help)
        self.gcode.register_command("IDM_ESTIMATE_BACKLASH",
                                    self.cmd_IDM_ESTIMATE_BACKLASH,
                                    desc=self.cmd_IDM_ESTIMATE_BACKLASH_help)
        self.gcode.register_command("probe", self.cmd_PROBE,
                                    desc=self.cmd_PROBE_help)
        self.gcode.register_command("PROBE_ACCURACY", self.cmd_PROBE_ACCURACY,
                                    desc=self.cmd_PROBE_ACCURACY_help)
        self.gcode.register_command('PROBE_CALIBRATE', self.cmd_PROBE_CALIBRATE,
                                    desc=self.cmd_PROBE_CALIBRATE_help)
        self.gcode.register_command('PROBE_SWITCH', self.cmd_PROBE_SWITCH,
                                    desc=self.cmd_PROBE_SWITCH_help)
        self.gcode.register_command("Z_OFFSET_APPLY_PROBE",
                                    self.cmd_Z_OFFSET_APPLY_PROBE,
                                    desc=self.cmd_Z_OFFSET_APPLY_PROBE_help)

    # Event handlers

    def _move(self, coord, speed):
        self.printer.lookup_object('toolhead').manual_move(coord, speed)
    cmd_PROBE_CALIBRATE_help = "Calibrate the probe's z_offset"

    def tap_probe(self, speed):
        toolhead = self.printer.lookup_object('toolhead')
        curtime = self.printer.get_reactor().monotonic()
        status = self.toolhead.get_kinematics().get_status(curtime)
        if 'z' not in toolhead.get_status(curtime)['homed_axes']:
            raise self.printer.command_error("Must home before probe")
        pos = toolhead.get_position()
        pos[2] = status["axis_minimum"][2]
        try:
            epos = self.phoming.probing_move(self.mcu_probe, pos, speed)
        except self.printer.command_error as e:
            reason = str(e)
            if "Timeout during endstop homing" in reason:
                reason += HINT_TIMEOUT
            raise self.printer.command_error(reason)
        self.gcode.respond_info("probe at %.3f,%.3f is z=%.6f"
                                % (epos[0], epos[1], epos[2] + self.z_offset))
        return epos[:3]
    def _calc_median(self, positions):
        z_sorted = sorted(positions, key=(lambda p: p[2]))
        middle = len(positions) // 2
        if (len(positions) & 1) == 1:
            # odd number of samples
            return z_sorted[middle]
        # even number of samples
        return self._calc_mean(z_sorted[middle-1:middle+1])
    def _calc_mean(self, positions):
        count = float(len(positions))
        return [sum([pos[i] for pos in positions]) / count
                for i in range(3)]
    def run_tap_probe(self, gcmd):
        speed = gcmd.get_float("PROBE_SPEED", self.probe_speed, above=0.)
        lift_speed = self.get_lift_speed(gcmd)
        sample_count = gcmd.get_int("SAMPLES", 4, minval=1)
        sample_retract_dist = gcmd.get_float("SAMPLE_RETRACT_DIST",
                                             5, above=0.)
        samples_tolerance = gcmd.get_float("SAMPLES_TOLERANCE",
                                           1, minval=0.)
        samples_retries = gcmd.get_int("SAMPLES_TOLERANCE_RETRIES",
                                       4, minval=0)
        samples_result = gcmd.get("SAMPLES_RESULT", "median")
        probexy = self.printer.lookup_object('toolhead').get_position()[:2]
        retries = 0
        positions = []
        while len(positions) < sample_count:
            # Probe position
            pos = self.tap_probe(speed)
            positions.append(pos)
            # Check samples tolerance
            z_positions = [p[2] for p in positions]
            if max(z_positions) - min(z_positions) > samples_tolerance:
                if retries >= samples_retries:
                    raise gcmd.error("Probe samples exceed samples_tolerance")
                gcmd.respond_info("Probe samples exceed tolerance. Retrying...")
                retries += 1
                positions = []
            # Retract
            if len(positions) < sample_count:
                self._move(probexy + [pos[2] + sample_retract_dist], lift_speed)
        # Calculate and return result
        if samples_result == 'median':
            return self._calc_median(positions)
        return self._calc_mean(positions)

    def probe_calibrate_finalize(self, kin_pos):
        if kin_pos is None:
            return
        z_offset = kin_pos[2] - self.probe_calibrate_z
        self.gcode.run_script_from_command("SET_GCODE_OFFSET Z_ADJUST=%s" % (z_offset))
        gcode_move = self.printer.lookup_object("gcode_move")
        offset = gcode_move.get_status()["homing_origin"].z
        configfile = self.printer.lookup_object('configfile')
        configfile.set("idm model " + self.model.name, 'model_offset', "%.3f" % (z_offset,))

    def cmd_PROBE_CALIBRATE(self, gcmd):
        if gcmd.get("METHOD","MANUAL").lower() == "auto":
            if self.calibration_method == "voron_tap":
                self.trigger_method = 2
            else:
                return
            #self.gcode.run_script_from_command("G28 Z")
            self._move([float(self.tap_location[0]), float(self.tap_location[1]), None], self.speed)
            curpos = self.run_tap_probe(gcmd)
            gcode_move = self.printer.lookup_object("gcode_move")
            offset = gcode_move.get_status()["homing_origin"].z
            self.probe_calibrate_z = offset - curpos[2]
            self.probe_calibrate_finalize([0,0,self.z_offset])
            self.trigger_method = 0
            curpos[2] = 5
            self._move(curpos, self.lift_speed)
            return
        self.trigger_method = 0
        manual_probe.verify_no_manual_probe(self.printer)
        lift_speed = self.get_lift_speed(gcmd)
        # Perform initial probe
        curpos = self.run_probe(gcmd)
        self.probe_calibrate_z = curpos[2] - self.trigger_distance
        # Move the nozzle over the probe point
        curpos[0] += self.x_offset
        curpos[1] += self.y_offset
        self._move(curpos, self.speed)
        # Start manual probe
        manual_probe.ManualProbeHelper(self.printer, gcmd,
                                       self.probe_calibrate_finalize)
    def _handle_connect(self):
        self.phoming = self.printer.lookup_object("homing")
        self.mod_axis_twist_comp = self.printer.lookup_object(
            "axis_twist_compensation", None
        )
        if self.mod_axis_twist_comp is not None:
            if not hasattr(self.mod_axis_twist_comp, "get_z_compensation_value"):
                self.raw_axis_twist_comp = self.mod_axis_twist_comp
                def get_z_compensation_value(self, pos):
                    temp = list(pos)
                    self.raw_axis_twist_comp._update_z_compensation_value(temp)
                    return temp[2]-pos[2]
                axis_twist_comp = type("class",(object,),{"get_z_compensation_value" : get_z_compensation_value, "raw_axis_twist_comp" : self.raw_axis_twist_comp})
                self.mod_axis_twist_comp = axis_twist_comp()
        # Ensure streaming mode is stopped
        self.idm_stream_cmd.send([0])

        self.model_temp = self.model_temp_builder.build_with_base(self)
        if self.model_temp:
            self.fmin = self.model_temp.fmin
        self.model = self.models.get(self.default_model_name, None)
        if self.model:
            self._apply_threshold()

    def _handle_mcu_identify(self):
        constants = self._mcu.get_constants()
        if self._mcu._mcu_freq < 20000000:
            self.sensor_freq =  self._mcu._mcu_freq
        elif self._mcu._mcu_freq < 100000000:
            self.sensor_freq = self._mcu._mcu_freq/2
        else:
            self.sensor_freq = self._mcu._mcu_freq/6
        self.inv_adc_max = 1.0 / constants.get("ADC_MAX")
        self.temp_smooth_count = constants.get("IDM_ADC_SMOOTH_COUNT")
        self.thermistor = thermistor.Thermistor(10000.0, 0.0)
        self.thermistor.setup_coefficients_beta(25., 47000.0, 4041.0)

        self.toolhead = self.printer.lookup_object("toolhead")
        self.trapq = self.toolhead.get_trapq()

    def _build_config(self):
        self.idm_stream_cmd = self._mcu.lookup_command(
            "idm_stream en=%u", cq=self.cmd_queue)
        self.idm_set_threshold = self._mcu.lookup_command(
            "idm_set_threshold trigger=%u untrigger=%u", cq=self.cmd_queue)
        self.idm_home_cmd = self._mcu.lookup_command(
            "idm_home trsync_oid=%c trigger_reason=%c trigger_invert=%c",
            cq=self.cmd_queue)
        self.idm_stop_home = self._mcu.lookup_command(
            "idm_stop_home", cq=self.cmd_queue)
        self.idm_base_read_cmd = self._mcu.lookup_query_command(
            "idm_base_read len=%c offset=%hu",
            "idm_base_data bytes=%*s offset=%hu",
            cq=self.cmd_queue)

    def stats(self, eventtime):
        return False, "%s: coil_temp=%.1f refs=%s" % (
            self.name,
            self.last_temp,
            self._stream_en,
        )

    # Virtual endstop

    def setup_pin(self, pin_type, pin_params):
        if pin_type != "endstop" or pin_params["pin"] != "z_virtual_endstop":
            raise pins.error("Probe virtual endstop only useful as endstop pin")
        if pin_params["invert"] or pin_params["pullup"]:
            raise pins.error("Can not pullup/invert probe virtual endstop")
        return self.mcu_probe

    # Probe interface

    def multi_probe_begin(self):
        self._start_streaming()

    def multi_probe_end(self):
        self._stop_streaming()

    def get_offsets(self):
        return self.x_offset, self.y_offset, self.trigger_distance

    def get_lift_speed(self, gcmd=None):
        if gcmd is not None:
            return gcmd.get_float("LIFT_SPEED", self.lift_speed, above=0.0)
        return self.lift_speed

    def run_probe(self, gcmd):
        if self.model is None:
            raise self.printer.command_error("No IDM model loaded")

        speed = gcmd.get_float("PROBE_SPEED", self.speed, above=0.0)
        allow_faulty = gcmd.get_int("ALLOW_FAULTY_COORDINATE", 0) != 0
        lift_speed = self.get_lift_speed(gcmd)
        toolhead = self.printer.lookup_object("toolhead")
        curtime = self.reactor.monotonic()
        if "z" not in toolhead.get_status(curtime)["homed_axes"]:
            raise self.printer.command_error("Must home before probe")

        self._start_streaming()
        try:
            epos = self._probe(speed, allow_faulty=allow_faulty)
            self.results.append(epos)
            return epos
        finally:
            self._stop_streaming()

    def _move_to_probing_height(self, speed):
        target = self.trigger_distance
        top = target + self.backlash_comp
        cur_z = self.toolhead.get_position()[2]
        if cur_z < top:
            self.toolhead.manual_move([None, None, top], speed)
        self.toolhead.manual_move([None, None, target], speed)
        self.toolhead.wait_moves()

    def _probing_move_to_probing_height(self, speed):
        curtime = self.reactor.monotonic()
        status = self.toolhead.get_kinematics().get_status(curtime)
        pos = self.toolhead.get_position()
        pos[2] = status["axis_minimum"][2]
        try:
            self.phoming.probing_move(self.mcu_probe, pos, speed)
            self._sample_printtime_sync(self.z_settling_time)
        except self.printer.command_error as e:
            reason = str(e)
            if "Timeout during probing move" in reason:
                reason += probe.HINT_TIMEOUT
            raise self.printer.command_error(reason)

    def _probe(self, speed, num_samples=10, allow_faulty=False):
        if self.trigger_method != 0:
            return self.tap_probe(speed)
        target = self.trigger_distance
        tdt = self.trigger_dive_threshold
        (dist, samples) = self._sample(5, num_samples)

        x, y = samples[0]["pos"][0:2]
        if self._is_faulty_coordinate(x, y, True):
            msg = "Probing within a faulty area"
            if not allow_faulty:
                raise self.printer.command_error(msg)
            else:
                self.gcode.respond_raw("!! " + msg + "\n")

        if dist > target + tdt:
            # If we are above the dive threshold right now, we'll need to
            # do probing move and then re-measure
            self._probing_move_to_probing_height(speed)
            (dist, samples) = self._sample(self.z_settling_time, num_samples)
        elif math.isinf(dist) and dist < 0:
            # We were below the valid range of the model
            msg = "Attempted to probe with IDM below calibrated model range"
            raise self.printer.command_error(msg)
        elif self.toolhead.get_position()[2] < target - tdt:
            # We are below the probing target height, we'll move to the
            # correct height and take a new sample.
            self._move_to_probing_height(speed)
            (dist, samples) = self._sample(self.z_settling_time, num_samples)
        pos = samples[0]["pos"]
        self.gcode.respond_info("probe at %.3f,%.3f,%.3f is z=%.6f"
                                % (pos[0], pos[1], pos[2], dist))

        return [pos[0], pos[1], pos[2] + target - dist]

    # Calibration routines

    def _start_calibration(self, gcmd):
        if self.calibration_method == "voron_tap":
            self.trigger_method = 2
        allow_faulty = gcmd.get_int("ALLOW_FAULTY_COORDINATE", 0) != 0
        if self.trigger_method != 0:
            self._move([float(self.tap_location[0]), float(self.tap_location[1]), None], self.speed)
            pos = self.toolhead.get_position()
            curtime = self.printer.get_reactor().monotonic()
            status = self.toolhead.get_kinematics().get_status(curtime)
            pos[2] = status["axis_maximum"][2]
            self.toolhead.set_position(pos, homing_axes=(0, 1, 2))
            self.tap_probe(self.probe_speed)
            pos[2] = - self.z_offset
            self.toolhead.set_position(pos)
            self._move([None, None, 0], self.lift_speed)
            kin = self.toolhead.get_kinematics()
            kin_spos = {s.get_name(): s.get_commanded_position()
                        for s in kin.get_steppers()}
            kin_pos = kin.calc_position(kin_spos)
            if self._is_faulty_coordinate(kin_pos[0], kin_pos[1]):
                msg = "Calibrating within a faulty area"
                if not allow_faulty:
                    raise gcmd.error(msg)
                else:
                    gcmd.respond_raw("!! " + msg + "\n")
            self._calibrate(gcmd, kin_pos, False)
            self.trigger_method = 0

        elif gcmd.get("SKIP_MANUAL_PROBE", None) is not None:
            kin = self.toolhead.get_kinematics()
            kin_spos = {s.get_name(): s.get_commanded_position()
                        for s in kin.get_steppers()}
            kin_pos = kin.calc_position(kin_spos)
            if self._is_faulty_coordinate(kin_pos[0], kin_pos[1]):
                msg = "Calibrating within a faulty area"
                if not allow_faulty:
                    raise gcmd.error(msg)
                else:
                    gcmd.respond_raw("!! " + msg + "\n")
            self._calibrate(gcmd, kin_pos, False)
        else:
            curtime = self.printer.get_reactor().monotonic()
            kin_status = self.toolhead.get_status(curtime)
            if "xy" not in kin_status["homed_axes"]:
                raise self.printer.command_error("Must home X and Y "
                                                 "before calibration")

            kin_pos = self.toolhead.get_position()
            if self._is_faulty_coordinate(kin_pos[0], kin_pos[1]):
                msg = "Calibrating within a faulty area"
                if not allow_faulty:
                    raise gcmd.error(msg)
                else:
                    gcmd.respond_raw("!! " + msg + "\n")

            forced_z = False
            if "z" not in kin_status["homed_axes"]:
                self.toolhead.get_last_move_time()
                pos = self.toolhead.get_position()
                pos[2] = (
                    kin_status["axis_maximum"][2]
                    - 2.0
                    - gcmd.get_float("CEIL", self.cal_ceil)
                )
                self.toolhead.set_position(pos, homing_axes=[2])
                forced_z = True

            cb = lambda kin_pos: self._calibrate(gcmd, kin_pos, forced_z)
            manual_probe.ManualProbeHelper(self.printer, gcmd, cb)

    def _calibrate(self, gcmd, kin_pos, forced_z):
        if kin_pos is None:
            if forced_z:
                kin = self.toolhead.get_kinematics()
                if hasattr(kin, "note_z_not_homed"):
                        kin.note_z_not_homed()
            return

        gcmd.respond_info("IDM calibration starting")
        cal_nozzle_z = gcmd.get_float("NOZZLE_Z", self.cal_nozzle_z)
        cal_floor = gcmd.get_float("FLOOR", self.cal_floor)
        cal_ceil = gcmd.get_float("CEIL", self.cal_ceil)
        cal_min_z = kin_pos[2] - cal_nozzle_z + cal_floor
        cal_max_z = kin_pos[2] - cal_nozzle_z + cal_ceil
        cal_speed = gcmd.get_float("SPEED", self.cal_speed)
        move_speed = gcmd.get_float("MOVE_SPEED", self.cal_move_speed)

        toolhead = self.toolhead
        curtime = self.reactor.monotonic()
        toolhead.wait_moves()
        pos = toolhead.get_position()

        # Move over to probe coordinate and pull out backlash
        curpos = self.toolhead.get_position()

        curpos[2] = cal_max_z + self.backlash_comp
        toolhead.manual_move(curpos, move_speed) # Up
        curpos[0] -= self.x_offset
        curpos[1] -= self.y_offset
        toolhead.manual_move(curpos, move_speed) # Over
        curpos[2] = cal_max_z
        toolhead.manual_move(curpos, move_speed) # Down
        toolhead.wait_moves()

        samples = []
        def cb(sample):
            samples.append(sample)

        try:
            self._start_streaming()
            self._sample_printtime_sync(50)
            with self.streaming_session(cb) as ss:
                self._sample_printtime_sync(50)
                toolhead.dwell(0.250)
                curpos[2] = cal_min_z
                toolhead.manual_move(curpos, cal_speed)
                toolhead.dwell(0.250)
                self._sample_printtime_sync(50)
        finally:
            self._stop_streaming()

        # Fit the sampled data
        z_offset = [s["pos"][2]-cal_min_z+cal_floor
                    for s in samples]
        freq = [s["freq"] for s in samples]
        temp = [s["temp"] for s in samples]
        inv_freq = [1/f for f in freq]
        poly = Polynomial.fit(inv_freq, z_offset, 9)
        temp_median = median(temp)
        self.model = IDMModel("default",
                                 self, poly, temp_median,
                                 min(z_offset), max(z_offset))
        self.models[self.model.name] = self.model
        self.model.save(self)
        self._apply_threshold()

        self.toolhead.get_last_move_time()
        pos = self.toolhead.get_position()
        pos[2] = cal_floor
        self.toolhead.set_position(pos)

        # Dump calibration curve
        fn = "/tmp/idm-calibrate-"+time.strftime("%Y%m%d_%H%M%S")+".csv"
        f = open(fn, "w")
        f.write("freq,z,temp\n")
        for i in range(len(freq)):
            f.write("%.5f,%.5f,%.3f\n" % (freq[i], z_offset[i], temp[i]))
        f.close()

        gcmd.respond_info("IDM calibrated at %.3f,%.3f from "
                          "%.3f to %.3f, speed %.2f mm/s, temp %.2fC"
                          % (pos[0], pos[1],
                          cal_min_z, cal_max_z, cal_speed, temp_median))
        self.trigger_method = 0
    # Internal

    def _update_thresholds(self, moving_up=False):
        self.trigger_freq = self.dist_to_freq(self.trigger_distance, self.last_temp)
        self.untrigger_freq = self.trigger_freq * (1-self.trigger_hysteresis)

    def _apply_threshold(self, moving_up=False):
        self._update_thresholds()
        trigger_c = int(self.freq_to_count(self.trigger_freq))
        untrigger_c = int(self.freq_to_count(self.untrigger_freq))
        self.idm_set_threshold.send([trigger_c, untrigger_c])

    def _register_model(self, name, model):
        if name in self.models:
            raise self.printer.config_error("Multiple IDM models with same"
                                            "name '%s'" % (name,))
        self.models[name] = model

    def _is_faulty_coordinate(self, x, y, add_offsets=False):
        if not self.mesh_helper:
            return False
        return self.mesh_helper._is_faulty_coordinate(x, y, add_offsets)

        # Streaming mode

    def _check_hardware(self, sample):
        if not self.hardware_failure:
            msg = None
            if sample["data"] == 0xFFFFFFF:
                msg = "coil is shorted or not connected"
            elif self.fmin is not None and sample["freq"] > 1.35 * self.fmin:
                msg = "coil expected max frequency exceeded"
            if msg:
                msg = "IDM hardware issue: " + msg
                self.hardware_failure = msg
                logging.error(msg)
                if self._stream_en:
                    self.printer.invoke_shutdown(msg)
                else:
                    self.gcode.respond_raw("!! " + msg + "\n")
        elif self._stream_en:
            self.printer.invoke_shutdown(self.hardware_failure)

    def _enrich_sample_time(self, sample):
        clock = sample["clock"] = self._mcu.clock32_to_clock64(sample["clock"])
        sample["time"] = self._mcu.clock_to_print_time(clock)

    def _enrich_sample_temp(self, sample):
        temp_adc = sample["temp"] / self.temp_smooth_count * self.inv_adc_max
        sample["temp"] = self.thermistor.calc_temp(temp_adc)

    def _enrich_sample_freq(self, sample):
        sample["data_smooth"] = self._data_filter.value()
        sample["freq"] = self.count_to_freq(sample["data_smooth"])
        self._check_hardware(sample)

    def _enrich_sample(self, sample):
        sample["dist"] = self.freq_to_dist(sample["freq"], sample["temp"])
        pos, vel = self._get_trapq_position(sample["time"])
        
        if pos is None:
            return
        if sample["dist"] is not None and self.mod_axis_twist_comp:
            sample["dist"] -= self.mod_axis_twist_comp.get_z_compensation_value(pos)
        sample["pos"] = pos
        sample["vel"] = vel

    def _start_streaming(self):
        if self._stream_en == 0:
            self.idm_stream_cmd.send([1])
            curtime = self.reactor.monotonic()
            self.reactor.update_timer(self._stream_timeout_timer,
                    curtime + STREAM_TIMEOUT)
        self._stream_en += 1
        self._data_filter.reset()
        self._stream_flush()
    def _stop_streaming(self):
        self._stream_en -= 1
        if self._stream_en == 0:
            self.reactor.update_timer(self._stream_timeout_timer,
                    self.reactor.NEVER)
            self.idm_stream_cmd.send([0])
        self._stream_flush()

    def _stream_timeout(self, eventtime):
        if not self._stream_en:
            return self.reactor.NEVER
        msg = "IDM sensor not receiving data"
        logging.error(msg)
        self.printer.invoke_shutdown(msg)
        return self.reactor.NEVER

    def request_stream_latency(self, latency):
        next_key = 0
        if self._stream_latency_requests:
            next_key = max(self._stream_latency_requests.keys()) + 1
        new_limit = STREAM_BUFFER_LIMIT_DEFAULT
        self._stream_latency_requests[next_key] = latency
        min_requested = min(self._stream_latency_requests.values())
        if min_requested < new_limit:
            new_limit = min_requested
        if new_limit < 1:
            new_limit = 1
        self._stream_buffer_limit_new = new_limit
        return next_key

    def drop_stream_latency_request(self, key):
        self._stream_latency_requests.pop(key, None)
        new_limit = STREAM_BUFFER_LIMIT_DEFAULT
        if self._stream_latency_requests:
            min_requested = min(self._stream_latency_requests.values())
            if min_requested < new_limit:
                new_limit = min_requested
        if new_limit < 1:
            new_limit = 1
        self._stream_buffer_limit_new = new_limit

    def streaming_session(self, callback, completion_callback=None, latency=None):
        return StreamingHelper(self, callback, completion_callback, latency)

    def _stream_flush(self):
        self._stream_flush_event.clear()
        while True:
            try:
                samples = self._stream_samples_queue.get_nowait()
                updated_timer = False
                for sample in samples:
                    if not updated_timer:
                        curtime = self.reactor.monotonic()
                        self.reactor.update_timer(self._stream_timeout_timer,
                                curtime + STREAM_TIMEOUT)
                        updated_timer = True

                    self._enrich_sample_temp(sample)
                    temp = sample["temp"]
                    if self.model_temp is not None and not (-40 < temp < 180):
                        msg = ("IDM temperature sensor faulty(read %.2f C),"
                                " disabling temperaure compensation" % (temp,))
                        logging.error(msg)
                        self.gcode.respond_raw("!! " + msg + "\n")
                        self.model_temp = None

                    self.last_temp = temp
                    if temp:
                        self.measured_min = min(self.measured_min, temp)
                        self.measured_max = max(self.measured_max, temp)

                    self._enrich_sample_time(sample)
                    self._data_filter.update(sample["time"], sample["data"])
                    self._enrich_sample_freq(sample)
                    self._enrich_sample(sample)

                    if len(self._stream_callbacks) > 0:
                        for cb in list(self._stream_callbacks.values()):
                            cb(sample)
                    last = sample
                if last is not None:
                    last = last.copy()
                    dist = last["dist"]
                    if dist is None or np.isinf(dist) or np.isnan(dist):
                        del last["dist"]
                    self.last_received_sample = last
            except queue.Empty:
                return

    def _stream_flush_schedule(self):
        force = self._stream_en == 0 # When streaming is disabled, let all through
        if self._stream_buffer_limit_new != self._stream_buffer_limit:
            force = True
            self._stream_buffer_limit = self._stream_buffer_limit_new
        if not force and len(self._stream_buffer) < self._stream_buffer_limit:
            return
        self._stream_samples_queue.put_nowait(self._stream_buffer)
        self._stream_buffer = []
        if self._stream_flush_event.is_set():
            return
        self._stream_flush_event.set()
        self.reactor.register_async_callback(lambda e: self._stream_flush())

    def _handle_idm_data(self, params):
        if self.trapq is None:
            return

        self._stream_buffer.append(params.copy())
        self._stream_flush_schedule()

    def _get_trapq_position(self, print_time):
        ffi_main, ffi_lib = chelper.get_ffi()
        data = ffi_main.new("struct pull_move[1]")
        count = ffi_lib.trapq_extract_old(self.trapq, data, 1, 0.0, print_time)
        if not count:
            return None, None
        move = data[0]
        move_time = max(0.0, min(move.move_t, print_time - move.print_time))
        dist = (move.start_v + .5 * move.accel * move_time) * move_time
        pos = (move.start_x + move.x_r * dist, move.start_y + move.y_r * dist,
               move.start_z + move.z_r * dist)
        velocity = move.start_v + move.accel * move_time
        return pos, velocity

    def _sample_printtime_sync(self, skip=0, count=1):
        move_time = self.toolhead.get_last_move_time()
        settle_clock = self._mcu.print_time_to_clock(move_time)
        samples = []
        total = skip + count

        def cb(sample):
            if sample["clock"] >= settle_clock:
                samples.append(sample)
                if len(samples) >= total:
                    raise StopStreaming

        with self.streaming_session(cb, latency=skip+count) as ss:
            ss.wait()

        samples = samples[skip:]

        if count == 1:
            return samples[0]
        else:
            return samples

    def _sample(self, skip, count):
        samples = self._sample_printtime_sync(skip, count)
        return (median([s["dist"] for s in samples]), samples)

    def _sample_async(self, count=1):
        samples = []
        def cb(sample):
            samples.append(sample)
            if len(samples) >= count:
                raise StopStreaming

        with self.streaming_session(cb, latency=count) as ss:
            ss.wait()

        if count == 1:
            return samples[0]
        else:
            return samples

    def count_to_freq(self, count):
        return count*self.sensor_freq/(2**28)

    def freq_to_count(self, freq):
        return freq*(2**28)/self.sensor_freq

    def dist_to_freq(self, dist, temp):
        if self.model is None:
            return None
        return self.model.dist_to_freq(dist, temp)

    def freq_to_dist(self, freq, temp):
        if self.model is None:
            return None
        return self.model.freq_to_dist(freq, temp)

    def get_status(self, eventtime):
        model = None
        if self.model is not None:
            model = self.model.name
        return {
            "last_sample": self.last_sample,
            "last_received_sample": self.last_received_sample,
            "model": model,
        }

    # Webhook handlers

    def _handle_req_status(self, web_request):
        temp = None
        sample = self._sample_async()
        out = {
            "freq": sample["freq"],
            "dist": sample["dist"],
        }
        temp = sample["temp"]
        if temp is not None:
            out["temp"] = temp
        web_request.send(out)

    def _handle_req_dump(self, web_request):
        self._api_dump_helper.add_client(web_request)

    # GCode command handlers
    cmd_PROBE_SWITCH_help = "swith between scan and tap"
    def cmd_PROBE_SWITCH(self, gcmd):
        method=gcmd.get("METHOD","NONE").lower()
        if method == "scan":
            self.trigger_method=0
            gcmd.respond_info("Method switched to SCAN")
        elif method == "voron_tap":
            self.trigger_method=2
            gcmd.respond_info("Method switched to VORON TAP")

    cmd_PROBE_help = "Probe Z-height at current XY position"
    def cmd_PROBE(self, gcmd):
        pos = self.run_probe(gcmd)
        gcmd.respond_info("Result is z=%.6f" % (pos[2],))

    cmd_IDM_CALIBRATE_help = "Calibrate idm response curve"
    def cmd_IDM_CALIBRATE(self,gcmd):
        self._start_calibration(gcmd)

    cmd_IDM_ESTIMATE_BACKLASH_help = "Estimate Z axis backlash"
    def cmd_IDM_ESTIMATE_BACKLASH(self, gcmd):
        # Get to correct Z height
        overrun = gcmd.get_float("OVERRUN", 1.0)
        speed = gcmd.get_float("PROBE_SPEED", self.speed, above=0.0)
        cur_z = self.toolhead.get_position()[2]
        self.toolhead.manual_move([None, None, cur_z+overrun], speed)
        self.run_probe(gcmd)

        lift_speed = self.get_lift_speed(gcmd)
        target = gcmd.get_float("Z", self.trigger_distance)

        num_samples = gcmd.get_int("SAMPLES", 20)
        wait = self.z_settling_time

        samples_up = []
        samples_down = []

        next_dir = -1

        try:
            self._start_streaming()

            (cur_dist, _samples) = self._sample(wait, 10)
            pos = self.toolhead.get_position()
            missing = target - cur_dist
            target = pos[2] + missing
            gcmd.respond_info("Target kinematic Z is %.3f" % (target,))

            if target - overrun < 0:
                raise gcmd.error("Target minus overrun must exceed 0mm")

            while len(samples_up) + len(samples_down) < num_samples:
                liftpos = [None, None, target + overrun * next_dir]
                self.toolhead.manual_move(liftpos, lift_speed)
                liftpos = [None, None, target]
                self.toolhead.manual_move(liftpos, lift_speed)
                self.toolhead.wait_moves()
                (dist, _samples) = self._sample(wait, 10)
                {-1: samples_up, 1: samples_down}[next_dir].append(dist)
                next_dir = next_dir * -1

        finally:
            self._stop_streaming()

        res_up = median(samples_up)
        res_down = median(samples_down)

        gcmd.respond_info("Median distance moving up %.5f, down %.5f, "
                          "delta %.5f over %d samples" %
                          (res_up, res_down, res_down - res_up,
                           num_samples))

    cmd_IDM_QUERY_help = "Take a sample from the sensor"
    def cmd_IDM_QUERY(self, gcmd):
        sample = self._sample_async()
        last_value = sample["freq"]
        dist = sample["dist"]
        temp = sample["temp"]
        self.last_sample = {
            "time": sample["time"],
            "value": last_value,
            "temp": temp,
            "dist": None if dist is None or np.isinf(dist) or np.isnan(dist) else dist,
        }
        if dist is None:
            gcmd.respond_info("Last reading: %.2fHz, %.2fC, no model" %
                              (last_value, temp,))
        else:
            gcmd.respond_info("Last reading: %.2fHz, %.2fC, %.5fmm" %
                              (last_value, temp, dist))

    cmd_IDM_STREAM_help = "Enable IDM Streaming"
    def cmd_IDM_STREAM(self, gcmd):
        if self._log_stream is not None:
            self._log_stream.stop()
            self._log_stream = None
            gcmd.respond_info("IDM Streaming disabled")
        else:
            f = None
            completion_cb = None
            fn = gcmd.get("FILENAME")
            f = open(fn, "w")
            def close_file():
                f.close()
            completion_cb = close_file
            f.write("time,data,data_smooth,freq,dist,temp,pos_x,pos_y,pos_z,vel\n")

            def cb(sample):
                pos = sample.get("pos", None)
                obj = "%.4f,%d,%.2f,%.5f,%.5f,%.2f,%s,%s,%s,%s\n" % (
                    sample["time"],
                    sample["data"],
                    sample["data_smooth"],
                    sample["freq"],
                    sample["dist"],
                    sample["temp"],
                    "%.3f" % (pos[0],) if pos is not None else "",
                    "%.3f" % (pos[1],) if pos is not None else "",
                    "%.3f" % (pos[2],) if pos is not None else "",
                    "%.3f" % (sample["vel"],) if "vel" in sample else ""
                )
                f.write(obj)

            self._log_stream = self.streaming_session(cb, completion_cb)
            gcmd.respond_info("IDM Streaming enabled")

    cmd_PROBE_ACCURACY_help = "Probe Z-height accuracy at current XY position"
    def cmd_PROBE_ACCURACY(self, gcmd):
        speed = gcmd.get_float("PROBE_SPEED", self.speed, above=0.0)
        lift_speed = self.get_lift_speed(gcmd)
        sample_count = gcmd.get_int("SAMPLES", 10, minval=1)
        sample_retract_dist = gcmd.get_float("SAMPLE_RETRACT_DIST", 5)
        allow_faulty = gcmd.get_int("ALLOW_FAULTY_COORDINATE", 0) != 0
        pos = self.toolhead.get_position()
        gcmd.respond_info("PROBE_ACCURACY at X:%.3f Y:%.3f Z:%.3f"
                          " (samples=%d retract=%.3f"
                          " speed=%.1f lift_speed=%.1f)\n"
                          % (pos[0], pos[1], pos[2],
                             sample_count, sample_retract_dist,
                             speed, lift_speed))

        start_height = self.trigger_distance + sample_retract_dist
        liftpos = [None, None, start_height]
        if self.trigger_method == 0:
            self.toolhead.manual_move(liftpos, lift_speed)
            self.multi_probe_begin()
            positions = []
            while len(positions) < sample_count:
                pos = self._probe(speed, allow_faulty=allow_faulty)
                positions.append(pos)
                self.toolhead.manual_move(liftpos, lift_speed)
            self.multi_probe_end()
        else:
            positions = []
            while len(positions) < sample_count:
                pos = self.tap_probe(speed)
                self.toolhead.manual_move(liftpos, lift_speed)
                positions.append(pos)
        zs = [p[2] for p in positions]
        max_value = max(zs)
        min_value = min(zs)
        range_value = max_value - min_value
        avg_value = sum(zs) / len(positions)
        median_ = median(zs)

        deviation_sum = 0
        for i in range(len(zs)):
            deviation_sum += pow(zs[2] - avg_value, 2.)
        sigma = (deviation_sum / len(zs)) ** 0.5

        gcmd.respond_info(
            "probe accuracy results: maximum %.6f, minimum %.6f, range %.6f, "
            "average %.6f, median %.6f, standard deviation %.6f" % (
            max_value, min_value, range_value, avg_value, median_, sigma))

    cmd_Z_OFFSET_APPLY_PROBE_help = "Adjust the probe's z_offset"
    def cmd_Z_OFFSET_APPLY_PROBE(self, gcmd):
        gcode_move = self.printer.lookup_object("gcode_move")
        offset = gcode_move.get_status()["homing_origin"].z

        if offset == 0:
            self.gcode.respond_info("Nothing to do: Z Offset is 0")
            return

        if not self.model:
            raise self.gcode.error("You must calibrate your model first, "
                                   "use IDM_CALIBRATE.")

        # We use the model code to save the new offset, but we can't actually
        # apply that offset yet because the gcode_offset is still in effect.
        # If the user continues to do stuff after this, the newly set model
        # offset would compound with the gcode offset. To ensure this doesn't
        # happen, we revert to the old model offset afterwards.
        # Really, the user should just be calling `SAVE_CONFIG` now.
        old_offset = self.model.offset
        self.model.offset += offset
        self.model.save(self, False)
        gcmd.respond_info(f"IDM model offset has been updated to {self.model.offset}.\n"
                "You must run the SAVE_CONFIG command now to update the\n"
                "printer config file and restart the printer.")
        self.model.offset = old_offset

class IDMModel:
    @classmethod
    def load(cls, name, config, idm):
        coef = config.getfloatlist("model_coef")
        temp = config.getfloat("model_temp")
        domain = config.getfloatlist("model_domain", count=2)
        [min_z, max_z] = config.getfloatlist("model_range", count=2)
        offset = config.getfloat("model_offset", 0.0)
        poly = Polynomial(coef, domain)
        return IDMModel(name, idm, poly, temp, min_z, max_z, offset)

    def __init__(self, name, idm, poly, temp, min_z, max_z, offset=0):
        self.name = name
        self.idm = idm
        self.poly = poly
        self.min_z = min_z
        self.max_z = max_z
        self.temp = temp
        self.offset = offset

    def save(self, idm, show_message=True):
        configfile = idm.printer.lookup_object("configfile")
        section = "idm model " + self.name
        configfile.set(section, "model_coef",
                       ",\n  ".join(map(str, self.poly.coef)))
        configfile.set(section, "model_domain",
                       ",".join(map(str, self.poly.domain)))
        configfile.set(section, "model_range",
                       "%f,%f" % (self.min_z, self.max_z))
        configfile.set(section, "model_temp",
                       "%f" % (self.temp))
        configfile.set(section, "model_offset", "%.5f" % (self.offset,))
        if show_message:
            idm.gcode.respond_info("IDM calibration for model '%s' has "
                    "been updated\nfor the current session. The SAVE_CONFIG "
                    "command will\nupdate the printer config file and restart "
                    "the printer." % (self.name,))

    def freq_to_dist_raw(self, freq):
        [begin, end] = self.poly.domain
        invfreq = 1/freq
        if invfreq > end:
            return float("inf")
        elif invfreq < begin:
            return float("-inf")
        else:
            return float(self.poly(invfreq) - self.offset)

    def freq_to_dist(self, freq, temp):
        if self.temp is not None and \
            self.idm.model_temp is not None:
            freq = self.idm.model_temp.compensate(
                            freq, temp, self.temp)
        return self.freq_to_dist_raw(freq)

    def dist_to_freq_raw(self, dist, max_e=0.00000001):
        if dist < self.min_z or dist > self.max_z:
            msg = ("Attempted to map out-of-range distance %f, valid range "
                    "[%.3f, %.3f]" % (dist, self.min_z, self.max_z))
            raise self.idm.printer.command_error(msg)
        dist += self.offset
        [begin, end] = self.poly.domain
        for _ in range(0, 50):
            f = (end + begin) / 2
            v = self.poly(f)
            if abs(v-dist) < max_e:
                return float(1.0 /f)
            elif v < dist:
                begin = f
            else:
                end = f
        raise self.idm.printer.command_error(
                "IDM model convergence error")

    def dist_to_freq(self, dist, temp, max_e=0.00000001):
        freq = self.dist_to_freq_raw(dist, max_e)
        if self.temp is not None and \
            self.idm.model_temp is not None:
            freq = self.idm.model_temp.compensate(
                            freq, self.temp, temp)
        return freq

class IDMTempModelBuilder:
    _DEFAULTS = {"a_a": None,
                "a_b": None,
                "b_a": None,
                "b_b": None,
                "fmin" : None,
                "fmin_temp" : None}

    @classmethod
    def load(cls, config):
        return IDMTempModelBuilder(config)

    def __init__(self, config):
        self.parameters = IDMTempModelBuilder._DEFAULTS.copy()
        for key in self.parameters.keys():
            param = config.getfloat("tc_" + key, None)
            if param is not None:
                self.parameters[key] = param

    def build(self):
        if self.parameters["fmin"] is None or \
            self.parameters["fmin_temp"] is None:
            return None
        logging.info("idm: built tempco model %s", self.parameters)
        return IDMTempModel(**self.parameters)

    def build_with_base(self, idm):
        base_data = idm.idm_base_read_cmd.send([6, 0])
        (f_count, adc_count) = struct.unpack("<IH", base_data["bytes"])
        if f_count < 0xFFFFFFFF and adc_count < 0xFFFF:
            if self.parameters["fmin"] is None:
                self.parameters["fmin"] = idm.count_to_freq(f_count)
                logging.info("idm: loaded fmin=%.2f from base",
                    self.parameters["fmin"])
            if self.parameters["fmin_temp"] is None:
                temp_adc = float(adc_count) / idm.temp_smooth_count * \
                    idm.inv_adc_max
                self.parameters["fmin_temp"] = \
                    idm.thermistor.calc_temp(temp_adc)
                logging.info("idm: loaded fmin_temp=%.2f from base",
                    self.parameters["fmin_temp"])
        else:
            logging.info("idm: fmin parameters not found in base")
        return self.build()

class IDMTempModel:
    def __init__(self, a_a, a_b, b_a, b_b, fmin, fmin_temp):
        self.a_a = a_a
        self.a_b = a_b
        self.b_a = b_a
        self.b_b = b_b
        self.fmin = fmin
        self.fmin_temp = fmin_temp
    def param_linear(self,x,a,b):
        return a*x+b
    def compensate(self, freq, temp_source, temp_target, tctl=None):
        if self.a_a == None or self.a_b == None or self.b_a == None or self.b_b == None:
            return freq
        A=4*(temp_source*self.a_a)**2+4*temp_source*self.a_a*self.b_a+self.b_a**2+4*self.a_a
        B=8*temp_source**2*self.a_a*self.a_b+4*temp_source*(self.a_a*self.b_b+self.a_b*self.b_a)+2*self.b_a*self.b_b+4*self.a_b-4*(freq-self.fmin)*self.a_a
        C=4*(temp_source*self.a_b)**2+4*temp_source*self.a_b*self.b_b+self.b_b**2-4*(freq-self.fmin)*self.a_b
        if(B**2-4*A*C<0):
            param_c=freq-self.param_linear(freq-self.fmin,self.a_a,self.a_b)*temp_source**2-self.param_linear(freq-self.fmin,self.b_a,self.b_b)*temp_source
            return self.param_linear(freq-self.fmin,self.a_a,self.a_b)*temp_target**2+self.param_linear(freq-self.fmin,self.b_a,self.b_b)*temp_target+param_c
        ax=(np.sqrt(B**2-4*A*C)-B)/2/A
        param_a=self.param_linear(ax,self.a_a,self.a_b)
        param_b=self.param_linear(ax,self.b_a,self.b_b)
        return param_a*(temp_target+param_b/2/param_a)**2+ax+self.fmin

class ModelManager:
    def __init__(self, idm):
        self.idm = idm
        self.gcode = idm.printer.lookup_object("gcode")
        self.gcode.register_command("IDM_MODEL_SELECT",
                                    self.cmd_IDM_MODEL_SELECT,
                                    desc=self.cmd_IDM_MODEL_SELECT_help)
        self.gcode.register_command("IDM_MODEL_SAVE",
                                    self.cmd_IDM_MODEL_SAVE,
                                    desc=self.cmd_IDM_MODEL_SAVE_help)
        self.gcode.register_command("IDM_MODEL_REMOVE",
                                    self.cmd_IDM_MODEL_REMOVE,
                                    desc=self.cmd_IDM_MODEL_REMOVE_help)
        self.gcode.register_command("IDM_MODEL_LIST",
                                    self.cmd_IDM_MODEL_LIST,
                                    desc=self.cmd_IDM_MODEL_LIST_help)

    cmd_IDM_MODEL_SELECT_help = "Load named idm model"
    def cmd_IDM_MODEL_SELECT(self, gcmd):
        name = gcmd.get("NAME")
        model = self.idm.models.get(name, None)
        if model is None:
            raise gcmd.error("Unknown model '%s'" % (name,))
        self.idm.model = model
        gcmd.respond_info("Selected IDM model '%s'" % (name,))

    cmd_IDM_MODEL_SAVE_help = "Save current idm model"
    def cmd_IDM_MODEL_SAVE(self, gcmd):
        model = self.idm.model
        if model is None:
            raise gcmd.error("No model currently selected")
        oldname = model.name
        name = gcmd.get("NAME", oldname)
        if name != oldname:
            model = copy.copy(model)
        model.name = name
        model.save(self.idm)
        if name != oldname:
            self.idm.models[name] = model

    cmd_IDM_MODEL_REMOVE_help = "Remove saved idm model"
    def cmd_IDM_MODEL_REMOVE(self, gcmd):
        name = gcmd.get("NAME")
        model = self.idm.models.get(name, None)
        if model is None:
            raise gcmd.error("Unknown model '%s'" % (name,))
        configfile = self.idm.printer.lookup_object("configfile")
        section = "idm model " + model.name
        configfile.remove_section(section)
        self.idm.models.pop(name)
        gcmd.respond_info("Model '%s' was removed for the current session.\n"
                          "Run SAVE_CONFIG to update the printer configuration"
                          "and restart Klipper." % (name,))
        if self.idm.model == model:
            self.idm.model = None

    cmd_IDM_MODEL_LIST_help = "Remove saved idm model"
    def cmd_IDM_MODEL_LIST(self, gcmd):
        if not self.idm.models:
            gcmd.respond_info("No IDM models loaded")
            return
        gcmd.respond_info("List of loaded IDM models:")
        current_model = self.idm.model
        for _name, model in sorted(self.idm.models.items()):
            if model == current_model:
                gcmd.respond_info("- %s [active]" % (model.name,))
            else:
                gcmd.respond_info("- %s" % (model.name,))


class AlphaBetaFilter:
    def __init__(self, alpha, beta):
        self.alpha = alpha
        self.beta = beta
        self.reset()

    def reset(self):
        self.xl = None
        self.vl = 0
        self.tl = None

    def update(self, time, measurement):
        if self.xl == None:
            self.xl = measurement
        if self.tl is not None:
            dt = time - self.tl
        else:
            dt = 0
        self.tl = time
        xk = self.xl + self.vl * dt
        vk = self.vl
        rk = measurement - xk
        xk = xk + self.alpha * rk
        if dt > 0:
            vk = vk + self.beta / dt * rk
        self.xl = xk
        self.vl = vk
        return xk

    def value(self):
        return self.xl

class StreamingHelper:
    def __init__(self, idm, callback, completion_callback, latency):
        self.idm = idm
        self.cb = callback
        self.completion_cb = completion_callback
        self.completion = self.idm.reactor.completion()

        self.latency_key = None
        if latency is not None:
            self.latency_key = self.idm.request_stream_latency(latency)

        self.idm._stream_callbacks[self] = self._handle
        self.idm._start_streaming()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def _handle(self, sample):
        try:
            self.cb(sample)
        except StopStreaming:
            self.completion.complete(())

    def stop(self):
        if not self in self.idm._stream_callbacks:
            return
        del self.idm._stream_callbacks[self]
        self.idm._stop_streaming()
        if self.latency_key is not None:
            self.idm.drop_stream_latency_request(self.latency_key)
        if self.completion_cb is not None:
            self.completion_cb()

    def wait(self):
        self.completion.wait()
        self.stop()

class StopStreaming(Exception):
    pass


class APIDumpHelper:
    def __init__(self, idm):
        self.idm = idm
        self.clients = {}
        self.stream = None
        self.buffer = []
        self.fields = ["dist", "temp", "pos", "freq", "vel", "time"]

    def _start_stop(self):
        if not self.stream and self.clients:
            self.stream = self.idm.streaming_session(self._cb)
        elif self.stream is not None and not self.clients:
            self.stream.stop()
            self.stream = None

    def _cb(self, sample):
        tmp = [sample.get(key, None) for key in self.fields]
        self.buffer.append(tmp)
        if len(self.buffer) > 50:
            self._update_clients()

    def _update_clients(self):
        for cconn, template in list(self.clients.items()):
            if cconn.is_closed():
                del self.clients[cconn]
                self._start_stop()
                continue
            tmp = dict(template)
            tmp["params"] = self.buffer
            cconn.send(tmp)
        self.buffer = []

    def add_client(self, web_request):
        cconn = web_request.get_client_connection()
        template = web_request.get_dict("response_template", {})
        self.clients[cconn] = template
        self._start_stop()
        web_request.send({"header": self.fields})

class IDMProbeWrapper:
    def __init__(self, idm):
        self.idm = idm

    def multi_probe_begin(self):
        return self.idm.multi_probe_begin()
    def multi_probe_end(self):
        return self.idm.multi_probe_end()
    def get_offsets(self):
        return self.idm.get_offsets()
    def get_lift_speed(self, gcmd=None):
        return self.idm.get_lift_speed(gcmd)
    def run_probe(self, gcmd):
        return self.idm.run_probe(gcmd)
    def get_probe_params(self, gcmd=None):
        return {'probe_speed': self.idm.speed,
            'lift_speed': self.idm.lift_speed}
    def start_probe_session(self, gcmd):
        self.multi_probe_begin()
        self.idm.results=[]
        return self
    def end_probe_session(self):
        self.idm.results=[]
        self.multi_probe_end()
    def pull_probed_results(self):
        res = self.idm.results
        self.idm.results = []
        return res
class IDMTempWrapper:
    def __init__(self, idm):
        self.idm = idm

    def get_temp(self, eventtime):
        return self.idm.last_temp, 0

    def get_status(self, eventtime):
        return {
            "temperature": round(self.idm.last_temp, 2),
            "measured_min_temp": round(self.idm.measured_min, 2),
            "measured_max_temp": round(self.idm.measured_max, 2)
        }

TRSYNC_TIMEOUT = 0.025
TRSYNC_SINGLE_MCU_TIMEOUT = 0.250

class IDMEndstopWrapper:
    def __init__(self, idm):
        self.idm = idm
        self._mcu = idm._mcu

        ffi_main, ffi_lib = chelper.get_ffi()
        self._trdispatch = ffi_main.gc(ffi_lib.trdispatch_alloc(), ffi_lib.free)
        self._trsyncs = [MCU_trsync(self.idm._mcu, self._trdispatch)]

        printer = self.idm.printer
        printer.register_event_handler("klippy:mcu_identify",
                                       self._handle_mcu_identify)
        printer.register_event_handler("homing:home_rails_begin",
                                       self._handle_home_rails_begin)
        printer.register_event_handler("homing:home_rails_end",
                                       self._handle_home_rails_end)
        printer.register_event_handler("homing:homing_move_begin",
                                       self._handle_homing_move_begin)
        self.z_homed = False
        self.is_homing = False

    def _handle_mcu_identify(self):
        self.toolhead = self.idm.printer.lookup_object("toolhead")
        kin = self.toolhead.get_kinematics()
        for stepper in kin.get_steppers():
            if stepper.is_active_axis("z"):
                self.add_stepper(stepper)
                if self.idm.endstop_add_stepper is not None:
                    self.idm.endstop_add_stepper(stepper)

    def _handle_home_rails_begin(self, homing_state, rails):
        self.is_homing = False

    def _handle_home_rails_end(self, homing_state, rails):
        if self.idm.model is None and self.idm.trigger_method == 0:
            return

        if not self.is_homing:
            return

        if 2 not in homing_state.get_axes():
            return

        # After homing Z we perform a measurement and adjust the toolhead
        # kinematic position.
        if(self.idm.trigger_method != 0):
            homing_state.set_homed_position([None, None, -self.idm.z_offset])
            return
        (dist, samples) = self.idm._sample(self.idm.z_settling_time, 10)
        if math.isinf(dist):
            logging.error("Post-homing adjustment measured samples %s", samples)
            raise self.idm.printer.command_error(
                    "Toolhead stopped below model range")
        homing_state.set_homed_position([None, None, dist])

    def _handle_homing_move_begin(self, hmove):
        if self.idm.mcu_probe in hmove.get_mcu_endstops():
            etrsync = self._trsyncs[0]

    def get_mcu(self):
        return self._mcu

    def add_stepper(self, stepper):
        trsyncs = {trsync.get_mcu(): trsync for trsync in self._trsyncs}
        stepper_mcu = stepper.get_mcu()
        trsync = trsyncs.get(stepper_mcu)
        if trsync is None:
            trsync = MCU_trsync(stepper_mcu, self._trdispatch)
            self._trsyncs.append(trsync)
        trsync.add_stepper(stepper)
        # Check for unsupported multi-mcu shared stepper rails, duplicated
        # from MCU_endstop
        sname = stepper.get_name()
        if sname.startswith("stepper_"):
            for ot in self._trsyncs:
                for s in ot.get_steppers():
                    if ot is not trsync and s.get_name().startswith(sname[:9]):
                        cerror = self._mcu.get_printer().config_error
                        raise cerror("Multi-mcu homing not supported on"
                                     " multi-mcu shared axis")

    def get_steppers(self):
        return [s for trsync in self._trsyncs for s in trsync.get_steppers()]

    def home_start(self, print_time, sample_time, sample_count, rest_time,
                   triggered=True):
        if self.idm.trigger_method == 2:
            self.is_homing = True
            return self.idm.endstop_mcu_endstop.home_start(print_time, sample_time, sample_count, rest_time, triggered)
        if self.idm.model is None and self.idm.trigger_method == 0:
            raise self.idm.printer.command_error("No IDM model loaded")

        self.is_homing = True
        if self.idm.trigger_method == 0:
            self.idm._apply_threshold()
            self.idm._sample_async()
        clock = self._mcu.print_time_to_clock(print_time)
        rest_ticks = self._mcu.print_time_to_clock(print_time+rest_time) - clock
        self._rest_ticks = rest_ticks
        reactor = self._mcu.get_printer().get_reactor()
        self._trigger_completion = reactor.completion()
        expire_timeout = TRSYNC_TIMEOUT
        if len(self._trsyncs) == 1:
            expire_timeout = TRSYNC_SINGLE_MCU_TIMEOUT
        for i, trsync in enumerate(self._trsyncs):
            try:
                trsync.start(print_time, self._trigger_completion, expire_timeout)
            except TypeError:
                offset = float(i) / len(self._trsyncs)
                trsync.start(
                    print_time, offset, self._trigger_completion, expire_timeout
                )
        etrsync = self._trsyncs[0]
        ffi_main, ffi_lib = chelper.get_ffi()
        ffi_lib.trdispatch_start(self._trdispatch, etrsync.REASON_HOST_REQUEST)
        
        if self.idm.trigger_method != 0:
            return self._trigger_completion

        self.idm.idm_home_cmd.send([
            etrsync.get_oid(),
            etrsync.REASON_ENDSTOP_HIT,
            0,
        ])
        return self._trigger_completion

    def home_wait(self, home_end_time):
        if self.idm.trigger_method == 2:
            return self.idm.endstop_mcu_endstop.home_wait(home_end_time)
        etrsync = self._trsyncs[0]
        etrsync.set_home_end_time(home_end_time)
        if self._mcu.is_fileoutput():
            self._trigger_completion.complete(True)
        self._trigger_completion.wait()
        self.idm.idm_stop_home.send()
        ffi_main, ffi_lib = chelper.get_ffi()
        ffi_lib.trdispatch_stop(self._trdispatch)
        res = [trsync.stop() for trsync in self._trsyncs]
        if any([r == etrsync.REASON_COMMS_TIMEOUT for r in res]):
            return -1.0
        if res[0] != etrsync.REASON_ENDSTOP_HIT:
            return 0.0
        if self._mcu.is_fileoutput():
            return home_end_time
        return home_end_time

    def query_endstop(self, print_time):
        if self.idm.model is None:
            return 1
        clock = self._mcu.print_time_to_clock(print_time)
        sample = self.idm._sample_async()
        if self.idm.trigger_freq <= sample["freq"]:
            return 1
        else:
            return 0

    def get_position_endstop(self):
        return self.idm.trigger_distance

class IDMMeshHelper:
    @classmethod
    def create(cls, idm, config):
        if config.has_section("bed_mesh"):
            mesh_config = config.getsection("bed_mesh")
            if mesh_config.get("mesh_radius", None) is not None:
                return None  # Use normal bed meshing for round beds
            return IDMMeshHelper(idm, config, mesh_config)
        else:
            return None

    def __init__(self, idm, config, mesh_config):
        self.idm = idm
        self.scipy = None
        self.mesh_config = mesh_config
        self.bm = self.idm.printer.load_object(mesh_config, "bed_mesh")

        self.speed = mesh_config.getfloat("speed", 50.0, above=0.0,
                                          note_valid=False)
        self.def_min_x, self.def_min_y = mesh_config.getfloatlist("mesh_min",
            count=2, note_valid=False)
        self.def_max_x, self.def_max_y = mesh_config.getfloatlist("mesh_max",
            count=2, note_valid=False)
        self.def_res_x, self.def_res_y = mesh_config.getintlist("probe_count",
            count=2, note_valid=False)
        self.rri = mesh_config.getint("relative_reference_index", None,
            note_valid=False)
        self.zero_ref_pos = mesh_config.getfloatlist("zero_reference_position",
            None, count=2)
        self.zero_ref_pos_cluster_size = config.getfloat(
            "zero_reference_cluster_size", 1, minval=0)
        self.dir = config.getchoice("mesh_main_direction",
            {"x": "x", "X": "x", "y": "y", "Y": "y"}, "y")
        self.overscan = config.getfloat("mesh_overscan", -1, minval=0)
        self.cluster_size = config.getfloat("mesh_cluster_size", 1, minval=0)
        self.runs = config.getint("mesh_runs", 1, minval=1)
        self.adaptive_margin = mesh_config.getfloat(
            "adaptive_margin", 0, note_valid=False
        )
        
        if self.zero_ref_pos is not None and self.rri is not None:
            logging.info("IDM: both 'zero_reference_position' and "
                    "'relative_reference_index' options are specified. The"
                    " former will be used")
        
        self.faulty_region_= []
        self.faulty_regions = []
        for i in list(range(1, 100, 1)):
            start = mesh_config.getfloatlist("faulty_region_%d_min" % (i,), None,
                                        count=2)
            if start is None:
                break
            end = mesh_config.getfloatlist("faulty_region_%d_max" % (i,), count=2)
            x_min = min(start[0], end[0])
            x_max = max(start[0], end[0])
            y_min = min(start[1], end[1])
            y_max = max(start[1], end[1])
            self.faulty_regions.append(Region(x_min, x_max, y_min, y_max))
            self.faulty_region_.append([x_min, y_min, x_max, y_max])
        self.faulty_region_ = np.array(self.faulty_region_).T
        self.exclude_object = None
        self.idm.printer.register_event_handler(
            "klippy:connect", self._handle_connect
        )
        
        self.gcode = self.idm.printer.lookup_object("gcode")
        self.prev_gcmd = self.gcode.register_command("BED_MESH_CALIBRATE", None)
        self.gcode.register_command(
            "BED_MESH_CALIBRATE", self.cmd_BED_MESH_CALIBRATE,
            desc=self.cmd_BED_MESH_CALIBRATE_help)

        if self.overscan < 0:
            printer = self.idm.printer
            printer.register_event_handler("klippy:mcu_identify",
                                           self._handle_mcu_identify)

    cmd_BED_MESH_CALIBRATE_help = "Perform Mesh Bed Leveling"
    def cmd_BED_MESH_CALIBRATE(self, gcmd):
        method = gcmd.get("METHOD", "idm").lower()
        if method == "idm":
            self.calibrate(gcmd)
        else:
            self.prev_gcmd(gcmd)
    
    def _handle_connect(self):
        self.exclude_object = self.idm.printer.lookup_object("exclude_object", None)
        
    def _handle_mcu_identify(self):
        # Auto determine a safe overscan amount
        toolhead = self.idm.printer.lookup_object("toolhead")
        curtime = self.idm.reactor.monotonic()
        status = toolhead.get_kinematics().get_status(curtime)
        xo = self.idm.x_offset
        yo = self.idm.y_offset
        settings = {
            "x": {
                "range": [self.def_min_x-xo, self.def_max_x-xo],
                "machine": [status["axis_minimum"][0],
                            status["axis_maximum"][0]],
                "count": self.def_res_y,
            },
            "y": {
                "range": [self.def_min_y-yo, self.def_max_y-yo],
                "machine": [status["axis_minimum"][1],
                            status["axis_maximum"][1]],
                "count": self.def_res_x,
            }
        }[self.dir]

        r = settings["range"]
        m = settings["machine"]
        space = (r[1] - r[0]) / (float(settings["count"]-1))
        self.overscan = min([
            max(0, r[0]-m[0]),
            max(0, m[1]-r[1]),
            space+2.0, # A half circle with 2mm lead in/out
        ])

    def _generate_path(self):
        xo = self.idm.x_offset
        yo = self.idm.y_offset
        settings = {
            "x": {
                "range_aligned": [self.min_x-xo, self.max_x-xo],
                "range_perpendicular": [self.min_y-yo, self.max_y-yo],
                "count": self.res_y,
                "swap_coord": False,
            },
            "y": {
                "range_aligned": [self.min_y-yo, self.max_y-yo],
                "range_perpendicular": [self.min_x-xo, self.max_x-xo],
                "count": self.res_x,
                "swap_coord": True,
            }
        }[self.dir]

        # We build the path in "normalized" coordinates and then simply
        # swap x and y at the end if we need to
        begin_a, end_a = settings["range_aligned"]
        begin_p, end_p = settings["range_perpendicular"]
        swap_coord = settings["swap_coord"]
        step = (end_p - begin_p) / (float(settings["count"]-1))
        points = []
        corner_radius = min(step/2, self.overscan)
        for i in range(0, settings["count"]):
            pos_p = begin_p + step * i
            even = i % 2 == 0 # If even we are going "right", else "left'
            pa = (begin_a, pos_p) if even else (end_a, pos_p)
            pb = (end_a, pos_p) if even else (begin_a, pos_p)

            l = (pa,pb)

            if len(points) > 0 and corner_radius > 0:
                # We need to insert an overscan corner. Basically we insert
                # a rounded rectangle to smooth out the transition and retain
                # as much speed as we can.
                #
                #  ---|---<
                # /
                # |
                # \
                #  ---|--->
                #
                # We just need to draw the two 90 degree arcs. They contain
                # the endpoints of the lines connecting everything.
                if even:
                    center = begin_a - self.overscan + corner_radius
                    points += arc_points(center, pos_p - step + corner_radius,
                            corner_radius, -90, -90)
                    points += arc_points(center, pos_p - corner_radius,
                            corner_radius, -180, -90)
                else:
                    center = end_a + self.overscan - corner_radius
                    points += arc_points(center, pos_p - step + corner_radius,
                            corner_radius, -90, 90)
                    points += arc_points(center, pos_p - corner_radius,
                            corner_radius, 0, 90)

            points.append(l[0])
            points.append(l[1])

        if swap_coord:
            for i in range(len(points)):
                (x,y) = points[i]
                points[i] = (y,x)

        return points

    def calibrate(self, gcmd):
        self.min_x, self.min_y = coord_fallback(gcmd, "MESH_MIN", convert_float,
                self.def_min_x, self.def_min_y, lambda v, d: max(v, d))
        self.max_x, self.max_y = coord_fallback(gcmd, "MESH_MAX", convert_float,
                self.def_max_x, self.def_max_y, lambda v, d: min(v, d))
        self.res_x, self.res_y = coord_fallback(gcmd, "PROBE_COUNT", int,
                self.def_res_x, self.def_res_y, lambda v, _d: max(v, 3))
        self.profile_name = gcmd.get("PROFILE", "default")
        
        if self.min_x > self.max_x:
            self.min_x, self.max_x = (max(self.max_x, self.def_min_x),
                                      min(self.min_x, self.def_max_x))
        if self.min_y > self.max_y:
            self.min_y, self.max_y = (max(self.max_y, self.def_min_y),
                                      min(self.min_y, self.def_max_y))

        # If the user gave RRI _on gcode_ then use it, else use zero_ref_pos
        # if we have it, and finally use config RRI if we have it.
        rri = gcmd.get_int('RELATIVE_REFERENCE_INDEX', None)
        if rri is not None:
            self.zero_ref_mode = ("rri", rri)
        elif self.zero_ref_pos is not None:
            self.zero_ref_mode = ("pos", self.zero_ref_pos)
            self.zero_ref_val = None
            self.zero_ref_bin = []
        elif self.rri is not None:
            self.zero_ref_mode = ("rri", self.rri)
        else:
            self.zero_ref_mode = None
            
        # If the user requested adaptive meshing, try to shrink the values we just configured
        if gcmd.get_int("ADAPTIVE", 0):
            if self.exclude_object is not None:
                margin = gcmd.get_float("ADAPTIVE_MARGIN", self.adaptive_margin)
                self._shrink_to_excluded_objects(gcmd, margin)
            else:
                gcmd.respond_info(
                    "Requested adaptive mesh, but [exclude_object] is not enabled. Ignoring."
                )
        
        self.step_x = (self.max_x - self.min_x) / (self.res_x - 1)
        self.step_y = (self.max_y - self.min_y) / (self.res_y - 1)

        self.toolhead = self.idm.toolhead
        path = self._generate_path()

        probe_speed = gcmd.get_float("PROBE_SPEED", self.idm.speed, above=0.0)
        self.idm._move_to_probing_height(probe_speed)

        speed = gcmd.get_float("SPEED", self.speed, above=0.0)
        runs = gcmd.get_int("RUNS", self.runs, minval=1)

        try:
            self.idm._start_streaming()

            # Move to first location
            (x,y) = path[0]
            self.toolhead.manual_move([x, y, None], speed)
            self.toolhead.wait_moves()

            self.idm._sample_printtime_sync(5)
            clusters = self._sample_mesh(gcmd, path, speed, runs)
            
            if self.zero_ref_mode and self.zero_ref_mode[0] == "pos":
                # If we didn't collect anything, hop over to the zero point
                # and sample. Otherwise, grab the median of what we collected.
                if len(self.zero_ref_bin) == 0:
                    self._collect_zero_ref(speed, self.zero_ref_mode[1])
                else:
                    self.zero_ref_val = median(self.zero_ref_bin)

        finally:
            self.idm._stop_streaming()

        matrix = self._process_clusters(clusters, gcmd)
        self._apply_mesh(matrix, gcmd)

    def _shrink_to_excluded_objects(self, gcmd, margin):
        bound_min_x, bound_max_x = None, None
        bound_min_y, bound_max_y = None, None
        objects = self.exclude_object.get_status().get("objects", {})
        if len(objects) == 0:
            return

        for obj in objects:
            for point in obj["polygon"]:
                bound_min_x = opt_min(bound_min_x, point[0])
                bound_max_x = opt_max(bound_max_x, point[0])
                bound_min_y = opt_min(bound_min_y, point[1])
                bound_max_y = opt_max(bound_max_y, point[1])
        bound_min_x -= margin
        bound_max_x += margin
        bound_min_y -= margin
        bound_max_y += margin

        # Calculate original step size and apply the new bounds
        orig_span_x = self.max_x - self.min_x
        orig_span_y = self.max_y - self.min_y
        orig_step_x = orig_span_x / (self.res_x - 1)
        orig_step_y = orig_span_y / (self.res_y - 1)

        if bound_min_x >= self.min_x:
            self.min_x = bound_min_x
        if bound_max_x <= self.max_x:
            self.max_x = bound_max_x
        if bound_min_y >= self.min_y:
            self.min_y = bound_min_y
        if bound_max_y <= self.max_y:
            self.max_y = bound_max_y

        # Update resolution to retain approximately the same step size as before
        self.res_x = math.ceil(self.res_x * (self.max_x - self.min_x) / orig_span_x)
        self.res_y = math.ceil(self.res_y * (self.max_y - self.min_y) / orig_span_y)
        # Guard against bicubic interpolation with 3 points on one axis
        min_res = 3
        if max(self.res_x, self.res_y) > 6 and min(self.res_x, self.res_y) < 4:
            min_res = 4
        self.res_x = max(self.res_x, min_res)
        self.res_y = max(self.res_y, min_res)

        self.profile_name = None

    def _fly_path(self, path, speed, runs):
        # Run through the path
        for i in range(runs):
            p = path if i % 2 == 0 else reversed(path)
            for (x,y) in p:
                self.toolhead.manual_move([x, y, None], speed)
        self.toolhead.dwell(0.251)
        self.toolhead.wait_moves()

    def _collect_zero_ref(self, speed, coord):
        xo, yo = self.idm.x_offset, self.idm.y_offset
        (x, y) = coord
        self.toolhead.manual_move([x-xo, y-yo, None], speed)
        (dist, _samples) = self.idm._sample(50, 10)
        self.zero_ref_val = dist

    def _is_valid_position(self, x, y):
        return self.min_x <= x <= self.max_x and self.min_y <= y <= self.min_y

    def _is_faulty_coordinate(self, x, y, add_offsets=False):
        if add_offsets:
            xo, yo = self.idm.x_offset, self.idm.y_offset
            x += xo
            y += yo
        for r in self.faulty_regions:
            if r.is_point_within(x, y):
                return True
        return False
        
    def _sample_mesh(self, gcmd, path, speed, runs):
        cs = gcmd.get_float("CLUSTER_SIZE", self.cluster_size, minval=0.0)
        zcs = self.zero_ref_pos_cluster_size
        if not (self.zero_ref_mode and self.zero_ref_mode[0] == "pos"):
            zcs = 0

        min_x, min_y = self.min_x, self.min_y
        xo, yo = self.idm.x_offset, self.idm.y_offset

        clusters = {}
        total_samples = [0]
        invalid_samples = [0]

        def cb(sample):
            total_samples[0] += 1
            d = sample["dist"]
            (x, y, z) = sample["pos"]
            x += xo
            y += yo

            if d is None or math.isinf(d):
                if self._is_valid_position(x, y):
                    invalid_samples[0] += 1
                return

            # Calculate coordinate of the cluster we are in
            xi = int(round((x - min_x) / self.step_x))
            yi = int(round((y - min_y) / self.step_y))
            if xi < 0 or self.res_x <= xi or yi < 0 or self.res_y <= yi:
                return

            # If there's a cluster size limit, apply it here
            if cs > 0:
                xf = xi * self.step_x + min_x
                yf = yi * self.step_y + min_y
                dx = x - xf
                dy = y - yf
                dist = math.sqrt(dx*dx+dy*dy)
                if dist > cs:
                    return

            # If we are looking for a zero reference, check if we
            # are close enough and if so, add to the bin.
            if zcs > 0:
                dx = x - self.zero_ref_mode[1][0]
                dy = y - self.zero_ref_mode[1][1]
                dist = math.sqrt(dx*dx+dy*dy)
                if dist <= zcs:
                    self.zero_ref_bin.append(d)
            
            k = (xi, yi)

            if k not in clusters:
                clusters[k] = []
            clusters[k].append(d)

        with self.idm.streaming_session(cb) as ss:
            self._fly_path(path, speed, runs)

        gcmd.respond_info("Sampled %d total points over %d runs" %
                          (total_samples[0], runs))
        if invalid_samples[0]:
            gcmd.respond_info("!! Encountered %d invalid samples!" % (invalid_samples[0],))
        gcmd.respond_info("Samples binned in %d clusters" % (len(clusters),))

        return clusters

    def _process_clusters(self, raw_clusters, gcmd):
        parent_conn, child_conn = multiprocessing.Pipe()
        dump_file = gcmd.get("FILENAME", None)

        def do():
            try:
                child_conn.send((False, self._do_process_clusters(raw_clusters,dump_file)))
            except:
                child_conn.send((True, traceback.format_exc()))
            child_conn.close()

        child = multiprocessing.Process(target=do)
        child.daemon = True
        child.start()
        reactor = self.idm.reactor
        eventtime = reactor.monotonic()
        while child.is_alive():
            eventtime = reactor.pause(eventtime + 0.1)
        is_err, result = parent_conn.recv()
        child.join()
        parent_conn.close()
        if is_err:
            raise Exception("Error processing mesh: %s" % (result,))
        else:
            is_inner_err, inner_result = result
            if is_inner_err:
                raise gcmd.error(inner_result)
            else:
                return inner_result

    def _do_process_clusters(self, raw_clusters, dump_file):
        if dump_file:
            with open(dump_file, "w") as f:
                f.write("x,y,xp,xy,dist\n")
                for yi in range(self.res_y):
                    line = []
                    for xi in range(self.res_x):
                        cluster = raw_clusters.get((xi, yi), [])
                        xp = xi * self.step_x + self.min_x
                        yp = yi * self.step_y + self.min_y
                        for dist in cluster:
                            f.write("%d,%d,%f,%f,%f\n" % (xi, yi, xp, yp, dist))

        mask = self._generate_fault_mask()
        matrix, faulty_regions = self._generate_matrix(raw_clusters, mask)
        if len(faulty_regions) > 0:
            (error, interpolator_or_msg) = self._load_interpolator()
            if error:
                return (True, interpolator_or_msg)
            matrix = self._interpolate_faulty(
                matrix, faulty_regions, interpolator_or_msg
            )
        err = self._check_matrix(matrix)
        if err is not None:
            return (True, err)
        return (False, self._finalize_matrix(matrix))

    def _generate_fault_mask(self):
        if len(self.faulty_regions) == 0:
            return None
        mask = np.full((self.res_y, self.res_x), True)
        for r in self.faulty_regions:
            r_xmin = max(0,int(math.ceil((r.x_min - self.min_x) / self.step_x)))
            r_ymin = max(0,int(math.ceil((r.y_min - self.min_y) / self.step_y)))
            r_xmax = min(self.res_x-1,int(math.floor((r.x_max - self.min_x) / self.step_x)))
            r_ymax = min(self.res_y-1,int(math.floor((r.y_max - self.min_y) / self.step_y)))
            for y in range(r_ymin, r_ymax + 1):
                for x in range(r_xmin, r_xmax + 1):
                    mask[(y, x)] = False
        return mask

    def _generate_matrix(self, raw_clusters, mask):
        faulty_indexes = []
        matrix = np.empty((self.res_y, self.res_x))
        for (x, y), values in raw_clusters.items():
            if mask is None or mask[(y, x)]:
                matrix[(y, x)] = self.idm.trigger_distance - median(values)
            else:
                matrix[(y, x)] = np.nan
                faulty_indexes.append((y, x))
        return matrix, faulty_indexes

    def _load_interpolator(self):
        if not self.scipy:
            try:
                self.scipy = importlib.import_module("scipy")
            except ImportError:
                msg = (
                    "Could not load `scipy`. To install it, simply re-run "
                    "the IDM `install.sh` script. This module is required "
                    "when using faulty regions when bed meshing."
                )
                return (True, msg)
        if hasattr(self.scipy.interpolate, "RBFInterpolator"):

            def rbf_interp(points, values, faulty):
                return self.scipy.interpolate.RBFInterpolator(points, values, 64)(
                    faulty
                )

            return (False, rbf_interp)
        else:

            def linear_interp(points, values, faulty):
                return self.scipy.interpolate.griddata(
                    points, values, faulty, method="linear"
                )

            return (False, linear_interp)

    def _interpolate_faulty(self, matrix, faulty_indexes, interpolator):
        ys, xs = np.mgrid[0 : matrix.shape[0], 0 : matrix.shape[1]]
        points = np.array([ys.flatten(), xs.flatten()]).T
        values = matrix.reshape(-1)
        good = ~np.isnan(values)
        fixed = interpolator(points[good], values[good], faulty_indexes)
        matrix[tuple(np.array(faulty_indexes).T)] = fixed
        return matrix

    def _check_matrix(self, matrix):
        empty_clusters = []
        for yi in range(self.res_y):
            for xi in range(self.res_x):
                if np.isnan(matrix[(yi, xi)]):
                    xc = xi * self.step_x + self.min_x
                    yc = yi * self.step_y + self.min_y
                    empty_clusters.append("  (%.3f,%.3f)[%d,%d]" % (xc, yc, xi, yi))
        if empty_clusters:
            err = (
                "Empty clusters found\n"
                "Try increasing mesh cluster_size or slowing down.\n"
                "The following clusters were empty:\n"
            ) + "\n".join(empty_clusters)
            return err
        else:
            return None

    def _finalize_matrix(self, matrix):
        z_offset = None
        if self.zero_ref_mode and self.zero_ref_mode[0] == "rri":
            rri = self.zero_ref_mode[1]
            if rri < 0 or rri >= self.res_x * self.res_y:
                rri = None
            if rri is not None:
                rri_x = rri % self.res_x
                rri_y = int(math.floor(rri / self.res_x))
                z_offset = matrix[rri_y][rri_x]
        elif self.zero_ref_mode and self.zero_ref_mode[0] == "pos":
            z_offset = self.idm.trigger_distance - self.zero_ref_val

        if z_offset is not None:
            matrix = matrix - z_offset
        return matrix.tolist()

    def _apply_mesh(self, matrix, gcmd):
        params = self.bm.bmc.mesh_config
        params["min_x"] = self.min_x
        params["max_x"] = self.max_x
        params["min_y"] = self.min_y
        params["max_y"] = self.max_y
        params["x_count"] = self.res_x
        params["y_count"] = self.res_y
        try:
            mesh = bed_mesh.ZMesh(params)
        except TypeError:
            mesh = bed_mesh.ZMesh(params, self.profile_name)
        try:
            mesh.build_mesh(matrix)
        except bed_mesh.BedMeshError as e:
            raise self.gcode.error(str(e))
        self.bm.set_mesh(mesh)
        self.gcode.respond_info("Mesh calibration complete")
        if self.profile_name is not None:
            self.bm.save_profile(self.profile_name)

class Region:
    def __init__(self, x_min, x_max, y_min, y_max):
        self.x_min = x_min
        self.x_max = x_max
        self.y_min = y_min
        self.y_max = y_max

    def is_point_within(self, x, y):
        return ((x > self.x_min and x < self.x_max) and
                (y > self.y_min and y < self.y_max))

def arc_points(cx, cy, r, start_angle, span):
    # Angle delta is determined by a max deviation(md) from 0.1mm:
    #   r * versin(d_a) < md
    #   versin(d_a) < md/r
    #   d_a < arcversin(md/r)
    #   d_a < arccos(1-md/r)
    # We then determine how many of these we can fit in exactly
    # 90 degrees(rounding up) and then determining the exact
    # delta angle.
    start_angle = start_angle / 180.0 * math.pi
    span = span / 180.0 * math.pi
    d_a = math.acos(1 - 0.1 / r)
    cnt = int(math.ceil(abs(span) / d_a))
    d_a = span / float(cnt)

    points = []
    for i in range(cnt+1):
        ang = start_angle + d_a*float(i)
        x = cx + math.cos(ang)*r
        y = cy + math.sin(ang)*r
        points.append((x,y))

    return points

def convert_float(data):
    toFloat=float(data)
    if np.isinf(toFloat) or np.isnan(toFloat):
        raise ValueError(f"Convert error when trying to convert string \"{data}\" into float")
    return toFloat

def coord_fallback(gcmd, name, parse, def_x, def_y, map=lambda v, d: v):
    param = gcmd.get(name, None)
    if param is not None:
        try:
            x, y = [parse(p.strip()) for p in param.split(",", 1)]
            return map(x, def_x), map(y, def_y)
        except:
            raise gcmd.error("Unable to parse parameter '%s'" % (name,))
    else:
        return def_x, def_y

def median(samples):
    return float(np.median(samples))

def opt_min(a, b):
    if a is None:
        return b
    return min(a, b)


def opt_max(a, b):
    if a is None:
        return b
    return max(a, b)
    
def load_config(config):
    idm = IDMProbe(config)
    config.get_printer().add_object("probe", IDMProbeWrapper(idm))
    temp = IDMTempWrapper(idm)
    config.get_printer().add_object("temperature_sensor IDM_coil", temp)
    pheaters = idm.printer.load_object(config, "heaters")
    pheaters.available_sensors.append("temperature_sensor IDM_coil")
    return idm

def load_config_prefix(config):
    idm = config.get_printer().lookup_object("idm")
    name = config.get_name()
    if name.startswith("idm model "):
        name = name[10:]
        model = IDMModel.load(name, config, idm)
        idm._register_model(name, model)
        return model
    else:
        raise config.error("Unknown idm config directive '%s'" % (name[7:],))