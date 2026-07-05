#!/usr/bin/env python3
"""Decode and edit Tritium/Prohelion WaveSculptor 22 .cfg files.

The file format is not officially documented here. The offsets below come from
the team's old annotated breakdown plus validation against manufacturer default
and exported controller configs. Unknown bytes in the 1880-byte config body are
preserved on edit. Save-tool trailers after the body are reported but omitted
when writing.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


MAGIC = b"TRITIUM CONFIGURATION FILE"

GLOBAL_CHECKSUM_OFFSET = 0x22
GLOBAL_CHECKSUM_START = 0x24
GLOBAL_CHECKSUM_END = 0x64
CALIBRATION_CHECKSUM_OFFSET = 0x64
CALIBRATION_CHECKSUM_START = 0x66
CALIBRATION_CHECKSUM_END = 0x17C

MOTOR_RECORD0_OFFSET = 0x17C
MOTOR_STRIDE = 0x96
MOTOR_COUNT = 10
CONFIG_BODY_SIZE = MOTOR_RECORD0_OFFSET + MOTOR_STRIDE * MOTOR_COUNT
MOTOR_CHECKSUM_OFFSET = 0
MOTOR_CHECKSUM_END = MOTOR_STRIDE
THERMISTOR_REFERENCE_TEMPERATURE_K = 273.15 + 25.0


@dataclass(frozen=True)
class Field:
    name: str
    offset: int
    kind: str
    help: str = ""

    @property
    def size(self) -> int:
        return {
            "f32": 4,
            "u16": 2,
            "u32": 4,
            "bytes2": 2,
            "bytes4": 4,
            "str20": 20,
            "bytes8": 8,
            "bytes16": 16,
        }[self.kind]


GLOBAL_FIELDS = [
    Field("active_motor", 0x24, "u16", "active motor slot index"),
    Field("sine_current_limit_arms", 0x26, "f32", "sine-wave phase current limit"),
    Field("speed_limit_rpm", 0x2A, "f32", "motor speed limit"),
    Field("idc_limit_a", 0x2E, "f32", "DC bus current limit"),
    Field("phase_c_temp_limit_c", 0x32, "f32", "controller phase-C/heatsink temperature limit"),
    Field("max_bus_voltage_v", 0x36, "f32", "upper DC bus voltage limit"),
    Field("min_bus_voltage_v", 0x3A, "f32", "lower DC bus voltage limit"),
    Field("tyre_diameter_m", 0x3E, "f32", "tyre diameter"),
    Field("can_baud_enum", 0x42, "u32", "CAN baud enum observed as 4 for these files"),
    Field("base_address", 0x46, "u32", "controller CAN base address"),
    Field("driver_controls_base", 0x4A, "u32", "drive/power command CAN base address"),
    Field("vehicle_mass_kg", 0x4E, "f32", "vehicle mass used by the velocity loop"),
    Field("sensorless_accel_rate", 0x52, "f32", "velocity-loop sensorless acceleration rate"),
    Field("telem_send_measurements_mask", 0x56, "u32", "bitmask for telemetry send-measurement checkboxes"),
    Field("sixstep_current_limit_a", 0x5A, "f32", "six-step phase current limit"),
    Field("bms_master_address", 0x5E, "u32", "BMS master CAN address"),
    Field("listen_for_bms_master_packets", 0x62, "u16", "boolean flag for BMS master packet listening"),
    Field("serial_number", 0x66, "u32", "controller serial number; not covered by global checksum"),
    Field("base_voltage_v", 0x6A, "f32", "calibration/base voltage"),
    Field("base_current_a", 0x6E, "f32", "calibration/base current"),
    Field("base_frequency_hz", 0x72, "f32", "calibration/base frequency"),
    Field("bus_capacitance_f", 0x76, "f32", "controller DC-link capacitance"),
    Field("bus_overvoltage_v", 0x7A, "f32", "hardware/controller overvoltage threshold"),
    Field("hall_voltage_scale", 0x7E, "f32", "hall voltage calibration scale"),
    Field("hall_voltage_offset", 0x82, "f32", "hall voltage calibration offset"),
    Field("rail_15v_scale", 0x86, "f32", "15V rail calibration scale"),
    Field("rail_15v_offset", 0x8A, "f32", "15V rail calibration offset"),
    Field("rail_1v9_scale", 0x8E, "f32", "1.9V rail calibration scale"),
    Field("rail_1v9_offset", 0x92, "f32", "1.9V rail calibration offset"),
    Field("rail_3v3_scale", 0x96, "f32", "3.3V rail calibration scale"),
    Field("rail_3v3_offset", 0x9A, "f32", "3.3V rail calibration offset"),
    Field("bus_voltage_scale", 0x9E, "f32", "bus voltage calibration scale"),
    Field("bus_voltage_offset", 0xA2, "f32", "bus voltage calibration offset"),
    Field("phase_c_voltage_scale", 0xA6, "f32", "phase C voltage calibration scale"),
    Field("phase_c_voltage_offset", 0xAA, "f32", "phase C voltage calibration offset"),
    Field("phase_b_voltage_scale", 0xAE, "f32", "phase B voltage calibration scale"),
    Field("phase_b_voltage_offset", 0xB2, "f32", "phase B voltage calibration offset"),
    Field("phase_a_voltage_scale", 0xB6, "f32", "phase A voltage calibration scale"),
    Field("phase_a_voltage_offset", 0xBA, "f32", "phase A voltage calibration offset"),
    Field("arcp_rail_scale", 0xBE, "f32", "ARCP rail calibration scale"),
    Field("arcp_rail_offset", 0xC2, "f32", "ARCP rail calibration offset"),
    Field("bus_current_scale", 0xC6, "f32", "bus current calibration scale"),
    Field("bus_current_offset", 0xCA, "f32", "bus current calibration offset"),
    Field("phase_c_current_scale", 0xCE, "f32", "phase C current calibration scale"),
    Field("phase_c_current_offset", 0xD2, "f32", "phase C current calibration offset"),
    Field("phase_b_current_scale", 0xD6, "f32", "phase B current calibration scale"),
    Field("phase_b_current_offset", 0xDA, "f32", "phase B current calibration offset"),
    Field("phase_a_current_scale", 0xDE, "f32", "phase A current calibration scale"),
    Field("phase_a_current_offset", 0xE2, "f32", "phase A current calibration offset"),
    Field("over_current_setpoint_scale", 0xE6, "f32", "over-current setpoint calibration scale"),
    Field("over_current_setpoint_offset", 0xEA, "f32", "over-current setpoint calibration offset"),
    Field("dsp_temp_scale", 0xEE, "f32", "DSP temperature calibration scale"),
    Field("dsp_temp_offset", 0xF2, "f32", "DSP temperature calibration offset"),
    Field("phase_c_temp_scale", 0xF6, "f32", "phase C temperature calibration scale"),
    Field("phase_c_temp_offset", 0xFA, "f32", "phase C temperature calibration offset"),
    Field("capacitor_temp_scale", 0xFE, "f32", "capacitor temperature calibration scale"),
    Field("capacitor_temp_offset", 0x102, "f32", "capacitor temperature calibration offset"),
    Field("phase_b_temp_scale", 0x106, "f32", "phase B temperature calibration scale"),
    Field("phase_b_temp_offset", 0x10A, "f32", "phase B temperature calibration offset"),
    Field("phase_a_temp_scale", 0x10E, "f32", "phase A temperature calibration scale"),
    Field("phase_a_temp_offset", 0x112, "f32", "phase A temperature calibration offset"),
    Field("dsp_temp_thermistor_raw_coefficient", 0x116, "f32", "DSP temperature stored thermistor coefficient"),
    Field("dsp_temp_beta", 0x11A, "f32", "DSP temperature beta"),
    Field("dsp_temp_param_2", 0x11E, "f32", "DSP temperature raw parameter 2"),
    Field("dsp_temp_param_3", 0x122, "f32", "DSP temperature raw parameter 3"),
    Field("phase_c_temp_thermistor_raw_coefficient", 0x126, "f32", "phase C temperature stored thermistor coefficient"),
    Field("phase_c_temp_beta", 0x12A, "f32", "phase C temperature beta"),
    Field("phase_c_temp_param_2", 0x12E, "f32", "phase C temperature raw parameter 2"),
    Field("phase_c_temp_param_3", 0x132, "f32", "phase C temperature raw parameter 3"),
    Field("capacitor_temp_thermistor_raw_coefficient", 0x136, "f32", "capacitor temperature stored thermistor coefficient"),
    Field("capacitor_temp_beta", 0x13A, "f32", "capacitor temperature beta"),
    Field("capacitor_temp_param_2", 0x13E, "f32", "capacitor temperature raw parameter 2"),
    Field("capacitor_temp_param_3", 0x142, "f32", "capacitor temperature raw parameter 3"),
    Field("phase_b_temp_thermistor_raw_coefficient", 0x146, "f32", "phase B temperature stored thermistor coefficient"),
    Field("phase_b_temp_beta", 0x14A, "f32", "phase B temperature beta"),
    Field("phase_b_temp_param_2", 0x14E, "f32", "phase B temperature raw parameter 2"),
    Field("phase_b_temp_param_3", 0x152, "f32", "phase B temperature raw parameter 3"),
    Field("phase_a_temp_thermistor_raw_coefficient", 0x156, "f32", "phase A temperature stored thermistor coefficient"),
    Field("phase_a_temp_beta", 0x15A, "f32", "phase A temperature beta"),
    Field("phase_a_temp_param_2", 0x15E, "f32", "phase A temperature raw parameter 2"),
    Field("phase_a_temp_param_3", 0x162, "f32", "phase A temperature raw parameter 3"),
    Field("sw_over_current_a", 0x166, "f32", "software over-current limit"),
    Field("check_observer_against_halls", 0x16A, "u16", "observer-vs-halls check flag"),
    Field("engage_motor_speed_hz", 0x16C, "f32", "calibration-tab engage motor speed"),
    Field("disengage_motor_speed_hz", 0x170, "f32", "calibration-tab disengage motor speed"),
    Field("controller_kp_factor", 0x174, "f32", "controller Kp gain factor"),
    Field("controller_ki_factor", 0x178, "f32", "controller Ki gain factor"),
]

MOTOR_FIELDS = [
    Field("pole_pairs", 2, "u16", "motor pole-pair count"),
    Field("line_resistance_ohm", 4, "f32", "line-to-line resistance"),
    Field("line_inductance_h", 8, "f32", "line-to-line inductance"),
    Field("speed_constant", 12, "f32", "motor speed/back-EMF constant as used by Profinity"),
    Field("phase_sequence", 16, "u16", "phase sequence checkbox; 0 unchecked, 1 BC=>CB checked"),
    Field("motor_temp_cutout_c", 18, "f32", "motor temperature cutout"),
    Field("motor_temp_ramp_c", 22, "f32", "motor temperature ramp point"),
    Field("temp_scale", 26, "f32", "motor temperature scaling"),
    Field("temp_offset", 30, "f32", "motor temperature offset"),
    Field("thermistor_raw_coefficient", 34, "f32", "stored beta-equation coefficient A = Ro*exp(-Beta/T0)"),
    Field("thermistor_beta", 38, "f32", "thermistor beta"),
    Field("motor_reserved_raw", 42, "bytes8", "raw reserved bytes before the motor name"),
    Field("name", 50, "str20", "null-terminated ASCII motor slot name"),
    Field("hall_angle_0", 70, "f32", "hall/resolver angle table entry 0"),
    Field("hall_angle_1", 74, "f32", "hall/resolver angle table entry 1"),
    Field("hall_angle_2", 78, "f32", "hall/resolver angle table entry 2"),
    Field("hall_angle_3", 82, "f32", "hall/resolver angle table entry 3"),
    Field("hall_angle_4", 86, "f32", "hall/resolver angle table entry 4"),
    Field("hall_angle_5", 90, "f32", "hall/resolver angle table entry 5"),
    Field("hall_angle_6", 94, "f32", "hall/resolver angle table entry 6"),
    Field("hall_angle_7", 98, "f32", "hall/resolver angle table entry 7"),
    Field("hall_order_0", 102, "u16", "hall edge order entry 0"),
    Field("hall_order_1", 104, "u16", "hall edge order entry 1"),
    Field("hall_order_2", 106, "u16", "hall edge order entry 2"),
    Field("hall_order_3", 108, "u16", "hall edge order entry 3"),
    Field("hall_order_4", 110, "u16", "hall edge order entry 4"),
    Field("hall_order_5", 112, "u16", "hall edge order entry 5"),
    Field("hall_order_6", 114, "u16", "hall edge order entry 6"),
    Field("hall_order_7", 116, "u16", "hall edge order entry 7"),
    Field("ignore_halls_while_sensorless", 118, "u16", "boolean flag"),
    Field("sensorless_engage_hz", 120, "f32", "mechanical frequency to enter sensorless mode"),
    Field("sensorless_disengage_hz", 124, "f32", "mechanical frequency to leave sensorless mode"),
    Field("motor_type", 128, "u16", "0 BLDC, 1 induction, 2 likely IPM"),
    Field("rotor_resistance_ohm", 130, "f32", "induction/IPM Rotor R field; GUI displays mR"),
    Field("rotor_inductance_h", 134, "f32", "induction/IPM Rotor L field; GUI displays uH"),
    Field("min_id_apk", 138, "f32", "induction Min Id Apk; IPM Id0 A"),
    Field("max_id_apk", 142, "f32", "induction Max Id Apk; IPM Id m A/Atot"),
    Field("encoder_count", 146, "u16", "encoder/resolver count"),
    Field("encoder_reverse", 148, "u16", "boolean flag"),
]

GLOBAL_FIELD_MAP = {field.name: field for field in GLOBAL_FIELDS}
MOTOR_FIELD_MAP = {field.name: field for field in MOTOR_FIELDS}
MOTOR_VIRTUAL_FIELDS = [
    Field(
        "thermistor_ro_ohm",
        -1,
        "f32",
        "GUI-style thermistor Ro in ohms; stored as raw coefficient plus beta",
    ),
]
MOTOR_VIRTUAL_FIELD_MAP = {field.name: field for field in MOTOR_VIRTUAL_FIELDS}

RAIL_CHANNELS = {"rail_15v", "rail_1v9", "rail_3v3"}
VOLTAGE_CHANNELS = {
    "bus_voltage",
    "phase_c_voltage",
    "phase_b_voltage",
    "phase_a_voltage",
    "hall_voltage",
    "arcp_rail",
}
CURRENT_CHANNELS = {
    "bus_current",
    "phase_c_current",
    "phase_b_current",
    "phase_a_current",
    "over_current_setpoint",
}
TEMPERATURE_CHANNELS = {
    "dsp_temp",
    "phase_c_temp",
    "capacitor_temp",
    "phase_b_temp",
    "phase_a_temp",
}
CALIBRATION_CHANNELS = sorted(
    RAIL_CHANNELS | VOLTAGE_CHANNELS | CURRENT_CHANNELS | TEMPERATURE_CHANNELS
)
CAN_BAUD_RATES = {
    0: "50 kbps",
    1: "100 kbps",
    2: "125 kbps",
    3: "250 kbps",
    4: "500 kbps",
    5: "1000 kbps",
}
TELEM_MEASUREMENT_BITS = {
    0x0000_0002: "Errors/Limiters",
    0x0000_0004: "Bus Volt/Curr",
    0x0000_0008: "Velocity",
    0x0000_0010: "Phase Currents",
    0x0000_0020: "DQ Voltage",
    0x0000_0040: "DQ Current",
    0x0000_0080: "BEMF",
    0x0000_0100: "15V/Hall Voltage",
    0x0000_0200: "3.3/1.9 rails",
    0x0000_0800: "Phase A/Motor temp",
    0x0000_1000: "Phase B/DSP temp",
    0x0000_2000: "Phase C temp",
    0x0000_4000: "Odometer/BusAh",
    0x0080_0000: "Slip Speed",
}


class ConfigError(Exception):
    pass


class Ws22Config:
    def __init__(self, data: bytes | bytearray, source: str = "<memory>") -> None:
        raw = bytes(data)
        self.source = source
        self.source_size_bytes = len(raw)
        self.data = bytearray(raw[:CONFIG_BODY_SIZE])
        self.trailer = raw[CONFIG_BODY_SIZE:]
        self.validate_magic()
        if len(raw) < CONFIG_BODY_SIZE:
            raise ConfigError(
                f"{self.source}: configuration body is {len(raw)} bytes, "
                f"expected at least {CONFIG_BODY_SIZE}"
            )

    @classmethod
    def read(cls, path: Path) -> "Ws22Config":
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ConfigError(f"could not read {path}: {exc}") from exc
        return cls(data, str(path))

    def write(self, path: Path) -> None:
        try:
            path.write_bytes(bytes(self.data[:CONFIG_BODY_SIZE]))
        except OSError as exc:
            raise ConfigError(f"could not write {path}: {exc}") from exc

    def validate_magic(self) -> None:
        if not self.data.startswith(MAGIC):
            raise ConfigError(f"{self.source}: not a Tritium configuration file")

    def read_at(self, offset: int, kind: str) -> Any:
        size = Field("_", offset, kind).size
        if offset < 0 or offset + size > len(self.data):
            raise ConfigError(f"field at 0x{offset:x} does not fit in {len(self.data)} byte file")
        if kind == "f32":
            return struct.unpack_from("<f", self.data, offset)[0]
        if kind == "u16":
            return struct.unpack_from("<H", self.data, offset)[0]
        if kind == "u32":
            return struct.unpack_from("<I", self.data, offset)[0]
        if kind == "str20":
            raw = bytes(self.data[offset : offset + 20])
            raw = raw.split(b"\0", 1)[0]
            return raw.decode("ascii", "replace")
        if kind in ("bytes2", "bytes4", "bytes8", "bytes16"):
            return bytes(self.data[offset : offset + size]).hex()
        raise ConfigError(f"unsupported field kind {kind}")

    def write_at(self, offset: int, kind: str, value: Any) -> None:
        size = Field("_", offset, kind).size
        if offset < 0 or offset + size > len(self.data):
            raise ConfigError(f"field at 0x{offset:x} does not fit in {len(self.data)} byte file")
        if kind == "f32":
            struct.pack_into("<f", self.data, offset, float(value))
        elif kind == "u16":
            value = int(value)
            if not 0 <= value <= 0xFFFF:
                raise ConfigError(f"u16 value out of range: {value}")
            struct.pack_into("<H", self.data, offset, value)
        elif kind == "u32":
            value = int(value)
            if not 0 <= value <= 0xFFFF_FFFF:
                raise ConfigError(f"u32 value out of range: {value}")
            struct.pack_into("<I", self.data, offset, value)
        elif kind == "str20":
            encoded = str(value).encode("ascii")
            if len(encoded) > 19:
                raise ConfigError("motor slot names must be ASCII and at most 19 bytes")
            self.data[offset : offset + 20] = encoded + b"\0" * (20 - len(encoded))
        elif kind == "bytes2":
            raw = parse_hex_bytes(str(value), 2)
            self.data[offset : offset + 2] = raw
        elif kind == "bytes4":
            raw = parse_hex_bytes(str(value), 4)
            self.data[offset : offset + 4] = raw
        elif kind == "bytes8":
            raw = parse_hex_bytes(str(value), 8)
            self.data[offset : offset + 8] = raw
        elif kind == "bytes16":
            raw = parse_hex_bytes(str(value), 16)
            self.data[offset : offset + 16] = raw
        else:
            raise ConfigError(f"unsupported field kind {kind}")

    def get_global(self, name: str) -> Any:
        field = GLOBAL_FIELD_MAP[name]
        return self.read_at(field.offset, field.kind)

    def set_global(self, name: str, value: Any) -> None:
        field = GLOBAL_FIELD_MAP[name]
        self.write_at(field.offset, field.kind, coerce_value(value, field.kind))

    def slot_offset(self, slot: int) -> int:
        if not 0 <= slot < MOTOR_COUNT:
            raise ConfigError(f"motor slot must be 0..{MOTOR_COUNT - 1}, got {slot}")
        return MOTOR_RECORD0_OFFSET + slot * MOTOR_STRIDE

    def active_slot(self) -> int:
        active = self.get_global("active_motor")
        if not 0 <= active < MOTOR_COUNT:
            raise ConfigError(f"active_motor is out of range: {active}")
        return int(active)

    def resolve_slot(self, slot_text: str) -> int:
        if slot_text == "active":
            return self.active_slot()
        try:
            slot = int(slot_text, 0)
        except ValueError as exc:
            raise ConfigError(f"invalid motor slot {slot_text!r}") from exc
        if not 0 <= slot < MOTOR_COUNT:
            raise ConfigError(f"motor slot must be 0..{MOTOR_COUNT - 1}, got {slot}")
        return slot

    def get_motor(self, slot: int, name: str) -> Any:
        if name == "thermistor_ro_ohm":
            return self.motor_thermistor_ro_ohm(slot)
        field = MOTOR_FIELD_MAP[name]
        return self.read_at(self.slot_offset(slot) + field.offset, field.kind)

    def set_motor(self, slot: int, name: str, value: Any) -> None:
        if name == "thermistor_ro_ohm":
            beta = self.get_motor(slot, "thermistor_beta")
            self.write_at(
                self.slot_offset(slot) + MOTOR_FIELD_MAP["thermistor_raw_coefficient"].offset,
                "f32",
                thermistor_raw_from_ro(float(value), beta),
            )
            return
        if name == "thermistor_beta":
            current_ro = self.motor_thermistor_ro_ohm(slot)
            new_beta = float(value)
            field = MOTOR_FIELD_MAP[name]
            self.write_at(self.slot_offset(slot) + field.offset, field.kind, new_beta)
            self.write_at(
                self.slot_offset(slot) + MOTOR_FIELD_MAP["thermistor_raw_coefficient"].offset,
                "f32",
                thermistor_raw_from_ro(current_ro, new_beta),
            )
            return
        field = MOTOR_FIELD_MAP[name]
        self.write_at(self.slot_offset(slot) + field.offset, field.kind, coerce_value(value, field.kind))

    def motor_thermistor_ro_ohm(self, slot: int) -> float:
        raw = self.read_at(self.slot_offset(slot) + MOTOR_FIELD_MAP["thermistor_raw_coefficient"].offset, "f32")
        beta = self.read_at(self.slot_offset(slot) + MOTOR_FIELD_MAP["thermistor_beta"].offset, "f32")
        return thermistor_ro_from_raw(raw, beta)

    def copy_motor(self, source_slot: int, dest_slot: int) -> None:
        source = self.slot_offset(source_slot)
        dest = self.slot_offset(dest_slot)
        self.data[dest : dest + MOTOR_STRIDE] = self.data[source : source + MOTOR_STRIDE]

    def checksum_global(self) -> int:
        return sum(self.data[GLOBAL_CHECKSUM_START:GLOBAL_CHECKSUM_END]) & 0xFFFF

    def stored_global_checksum(self) -> int:
        return self.read_at(GLOBAL_CHECKSUM_OFFSET, "u16")

    def recompute_global_checksum(self) -> None:
        self.write_at(GLOBAL_CHECKSUM_OFFSET, "u16", self.checksum_global())

    def checksum_calibration(self) -> int:
        return sum(self.data[CALIBRATION_CHECKSUM_START:CALIBRATION_CHECKSUM_END]) & 0xFFFF

    def stored_calibration_checksum(self) -> int:
        return self.read_at(CALIBRATION_CHECKSUM_OFFSET, "u16")

    def recompute_calibration_checksum(self) -> None:
        self.write_at(CALIBRATION_CHECKSUM_OFFSET, "u16", self.checksum_calibration())

    def checksum_motor(self, slot: int) -> int:
        base = self.slot_offset(slot)
        skip = {base + MOTOR_CHECKSUM_OFFSET, base + MOTOR_CHECKSUM_OFFSET + 1}
        return sum(
            self.data[index]
            for index in range(base, base + MOTOR_CHECKSUM_END)
            if index not in skip
        ) & 0xFFFF

    def stored_motor_checksum(self, slot: int) -> int:
        return self.read_at(self.slot_offset(slot) + MOTOR_CHECKSUM_OFFSET, "u16")

    def recompute_motor_checksum(self, slot: int) -> None:
        self.write_at(self.slot_offset(slot) + MOTOR_CHECKSUM_OFFSET, "u16", self.checksum_motor(slot))

    def present_slots(self) -> list[int]:
        return list(range(MOTOR_COUNT))

    def trailer_raw(self) -> str:
        return self.trailer.hex()

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_size_bytes": self.source_size_bytes,
            "body_size_bytes": len(self.data),
            "output_size_bytes": CONFIG_BODY_SIZE,
            "global_checksum": checksum_dict(
                self.stored_global_checksum(), self.checksum_global()
            ),
            "calibration_checksum": checksum_dict(
                self.stored_calibration_checksum(), self.checksum_calibration()
            ),
            "trailer_raw": self.trailer_raw(),
            "globals": {field.name: self.read_at(field.offset, field.kind) for field in GLOBAL_FIELDS},
            "motor_slots": [self.motor_dict(slot) for slot in self.present_slots()],
        }

    def motor_dict(self, slot: int) -> dict[str, Any]:
        stored = self.stored_motor_checksum(slot)
        calculated = self.checksum_motor(slot)
        values = {}
        for field in MOTOR_FIELDS:
            values[field.name] = self.read_at(self.slot_offset(slot) + field.offset, field.kind)
        for field in MOTOR_VIRTUAL_FIELDS:
            values[field.name] = self.get_motor(slot, field.name)
        return {
            "slot": slot,
            "active": slot == self.active_slot(),
            "offset": self.slot_offset(slot),
            "checksum": checksum_dict(stored, calculated),
            **values,
        }


def checksum_dict(stored: int, calculated: int) -> dict[str, Any]:
    return {
        "stored": stored,
        "calculated": calculated,
        "valid": stored == calculated,
    }


def thermistor_raw_from_ro(ro_ohm: float, beta: float) -> float:
    if ro_ohm < 0:
        raise ConfigError(f"thermistor_ro_ohm must be non-negative, got {ro_ohm}")
    return ro_ohm * math.exp(-beta / THERMISTOR_REFERENCE_TEMPERATURE_K)


def thermistor_ro_from_raw(raw: float, beta: float) -> float:
    return float(raw) * math.exp(float(beta) / THERMISTOR_REFERENCE_TEMPERATURE_K)


def coerce_value(value: Any, kind: str) -> Any:
    if kind == "f32":
        return float(value)
    if kind in ("u16", "u32"):
        return int(str(value), 0)
    if kind == "str20":
        return str(value)
    if kind in ("bytes2", "bytes4", "bytes8", "bytes16"):
        return str(value)
    raise ConfigError(f"unsupported field kind {kind}")


def parse_hex_bytes(text: str, expected_len: int) -> bytes:
    cleaned = text.strip().replace(" ", "").replace("_", "")
    if cleaned.startswith("0x"):
        cleaned = cleaned[2:]
    if len(cleaned) != expected_len * 2:
        raise ConfigError(f"expected {expected_len} bytes of hex")
    try:
        return bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ConfigError(f"invalid hex bytes: {text}") from exc


def apply_assignment(config: Ws22Config, assignment: str) -> set[int]:
    if "=" not in assignment:
        raise ConfigError(f"assignment must be name=value: {assignment}")
    path, value = assignment.split("=", 1)
    parts = [part.strip() for part in path.split(".")]
    if len(parts) == 1:
        name = parts[0]
        if name not in GLOBAL_FIELD_MAP:
            raise ConfigError(f"unknown global field {parts[0]!r}")
        config.set_global(name, value)
        return set()
    if len(parts) == 3 and parts[0] == "motor":
        slot = config.resolve_slot(parts[1])
        field_name = parts[2]
        if field_name not in MOTOR_FIELD_MAP and field_name not in MOTOR_VIRTUAL_FIELD_MAP:
            raise ConfigError(f"unknown motor field {field_name!r}")
        config.set_motor(slot, field_name, value)
        return {slot}
    raise ConfigError(
        "field paths must be either field=value or motor.<slot|active>.field=value"
    )


def parse_motor_copy(config: Ws22Config, text: str) -> tuple[int, int]:
    if ":" not in text:
        raise ConfigError(f"motor copy must be FROM:TO, got {text!r}")
    source_text, dest_text = text.split(":", 1)
    return (config.resolve_slot(source_text), config.resolve_slot(dest_text))


def calibration_base(config: Ws22Config, channel: str, override: float | None = None) -> float:
    if override is not None:
        if override == 0:
            raise ConfigError("calibration base must be nonzero")
        return override
    if channel in RAIL_CHANNELS:
        return 15.0
    if channel in VOLTAGE_CHANNELS:
        base = config.get_global("base_voltage_v")
    elif channel in CURRENT_CHANNELS:
        base = config.get_global("base_current_a")
    elif channel in TEMPERATURE_CHANNELS:
        return 128.0
    else:
        raise ConfigError(f"unknown calibration channel {channel!r}")
    if base is None or base == 0:
        raise ConfigError(f"base value for {channel} is missing or zero")
    return float(base)


def parse_calibration_point(text: str) -> tuple[float, float]:
    for separator in ("=", ":"):
        if separator in text:
            reported_text, true_text = text.split(separator, 1)
            return (float(reported_text), float(true_text))
    raise ConfigError(f"calibration point must be REPORTED=TRUE, got {text!r}")


def solve_calibration(
    config: Ws22Config,
    channel: str,
    points: list[tuple[float, float]],
    mode: str,
    base_override: float | None = None,
) -> tuple[float, float, float, float]:
    if channel not in CALIBRATION_CHANNELS:
        raise ConfigError(
            f"unknown calibration channel {channel!r}; use one of {', '.join(CALIBRATION_CHANNELS)}"
        )
    if not points:
        raise ConfigError("at least one calibration point is required")

    scale_name = f"{channel}_scale"
    offset_name = f"{channel}_offset"
    old_scale = float(config.get_global(scale_name))
    old_offset = float(config.get_global(offset_name))
    if old_scale == 0:
        raise ConfigError(f"{scale_name} is zero; cannot infer raw ADC values")

    base = calibration_base(config, channel, base_override)
    raw_points = []
    for reported, true_value in points:
        reported_normalized = reported / base
        true_normalized = true_value / base
        raw = (reported_normalized - old_offset) / old_scale
        raw_points.append((raw, true_normalized))

    if mode == "fit":
        if len(raw_points) < 2:
            raise ConfigError("fit mode requires at least two REPORTED=TRUE points")
        mean_raw = sum(raw for raw, _ in raw_points) / len(raw_points)
        mean_true = sum(true for _, true in raw_points) / len(raw_points)
        denominator = sum((raw - mean_raw) ** 2 for raw, _ in raw_points)
        if denominator == 0:
            raise ConfigError("fit mode requires points with different reported values")
        new_scale = sum((raw - mean_raw) * (true - mean_true) for raw, true in raw_points) / denominator
        new_offset = mean_true - new_scale * mean_raw
    elif mode == "scale":
        new_offset = old_offset
        denominator = sum(raw * raw for raw, _ in raw_points)
        if denominator == 0:
            raise ConfigError("scale mode requires a nonzero inferred raw value")
        new_scale = sum(raw * (true - new_offset) for raw, true in raw_points) / denominator
    elif mode == "offset":
        new_scale = old_scale
        new_offset = sum(true - new_scale * raw for raw, true in raw_points) / len(raw_points)
    else:
        raise ConfigError(f"unsupported calibration mode {mode!r}")

    return (old_scale, old_offset, new_scale, new_offset)


def apply_edits(args: argparse.Namespace, config: Ws22Config) -> set[int]:
    touched_slots: set[int] = set()

    for copy_spec in args.copy_motor or []:
        source_slot, dest_slot = parse_motor_copy(config, copy_spec)
        config.copy_motor(source_slot, dest_slot)
        touched_slots.add(dest_slot)

    for assignment in args.set or []:
        touched_slots.update(apply_assignment(config, assignment))

    return touched_slots


def recompute_after_edits(
    config: Ws22Config,
    touched_slots: set[int],
    recompute_all_slot_checksums: bool,
    no_recompute_touched_slot_checksums: bool,
) -> set[int]:
    config.recompute_global_checksum()
    config.recompute_calibration_checksum()
    if recompute_all_slot_checksums:
        touched_slots.update(config.present_slots())
    if not no_recompute_touched_slot_checksums:
        for slot in sorted(touched_slots):
            config.recompute_motor_checksum(slot)
    return touched_slots


def format_value(name: str, value: Any) -> str:
    if value is None:
        return "<missing>"
    if name == "can_baud_enum" and isinstance(value, int):
        rate = CAN_BAUD_RATES.get(value)
        return f"{value} ({rate})" if rate else str(value)
    if name == "telem_send_measurements_mask" and isinstance(value, int):
        disabled = [label for bit, label in TELEM_MEASUREMENT_BITS.items() if not value & bit]
        summary = "all known enabled" if not disabled else "disabled: " + ", ".join(disabled)
        return f"0x{value:08x} ({summary})"
    if name == "listen_for_bms_master_packets" and isinstance(value, int):
        return f"{value} ({'enabled' if value else 'disabled'})"
    if name == "phase_sequence" and isinstance(value, int):
        if value == 0:
            return "0 (unchecked)"
        if value == 1:
            return "1 (BC=>CB checked)"
        return str(value)
    if (
        isinstance(value, int)
        and ("address" in name or name in {"base_address", "driver_controls_base"})
    ):
        return f"0x{value:x}"
    if name in {"line_resistance_ohm", "rotor_resistance_ohm"} and isinstance(value, float):
        return f"{value:.9g} ohm ({value * 1e3:.3f} mR)"
    if name in {"line_inductance_h", "rotor_inductance_h"} and isinstance(value, float):
        return f"{value:.9g} H ({value * 1e6:.3f} uH)"
    if (
        name == "thermistor_raw_coefficient"
        or name.endswith("_thermistor_raw_coefficient")
    ) and isinstance(value, float):
        return f"{value:.9g} (stored coefficient)"
    if name == "thermistor_ro_ohm" and isinstance(value, float):
        return f"{value:.9g} ohm"
    if name.startswith("unknown_") and isinstance(value, str):
        return " ".join(value[i : i + 2] for i in range(0, len(value), 2))
    if isinstance(value, float):
        if math.isfinite(value):
            return f"{value:.9g}"
        return str(value)
    return str(value)


def status_text(stored: int, calculated: int) -> str:
    status = "ok" if stored == calculated else "MISMATCH"
    return f"stored=0x{stored:04x} calculated=0x{calculated:04x} {status}"


def command_dump(args: argparse.Namespace) -> int:
    config = Ws22Config.read(args.input)
    if args.json:
        print(json.dumps(config.to_dict(), indent=2, sort_keys=True))
        return 0

    active = config.active_slot()
    active_name = config.get_motor(active, "name")
    print(f"{args.input}")
    print(f"body size: {len(config.data)} bytes")
    if config.source_size_bytes != len(config.data):
        print(f"source size: {config.source_size_bytes} bytes")
    if config.trailer_raw():
        print(
            "source trailer raw:",
            f"{format_value('unknown_trailer_raw', config.trailer_raw())} (omitted on write)",
        )
    print(
        "global checksum:",
        status_text(config.stored_global_checksum(), config.checksum_global()),
    )
    print(
        "calibration checksum:",
        status_text(config.stored_calibration_checksum(), config.checksum_calibration()),
    )
    print(f"active motor: {active} ({active_name})")
    print()
    print("globals:")
    for field in GLOBAL_FIELDS:
        print(f"  {field.name}: {format_value(field.name, config.read_at(field.offset, field.kind))}")

    print()
    print("motor slots:")
    for slot in config.present_slots():
        marker = "*" if slot == active else " "
        stored = config.stored_motor_checksum(slot)
        calculated = config.checksum_motor(slot)
        print(
            f"{marker} {slot}: {config.get_motor(slot, 'name')!r} "
            f"checksum {status_text(stored, calculated)}"
        )
        for name in [
            "motor_type",
            "rotor_resistance_ohm",
            "rotor_inductance_h",
            "min_id_apk",
            "max_id_apk",
            "pole_pairs",
            "encoder_count",
            "encoder_reverse",
            "ignore_halls_while_sensorless",
            "sensorless_engage_hz",
            "sensorless_disengage_hz",
            "line_resistance_ohm",
            "line_inductance_h",
            "speed_constant",
            "phase_sequence",
            "motor_temp_cutout_c",
            "motor_temp_ramp_c",
            "temp_scale",
            "temp_offset",
            "thermistor_ro_ohm",
            "thermistor_raw_coefficient",
            "thermistor_beta",
        ]:
            print(f"    {name}: {format_value(name, config.get_motor(slot, name))}")
    return 0


def command_check(args: argparse.Namespace) -> int:
    config = Ws22Config.read(args.input)
    ok = True
    stored = config.stored_global_checksum()
    calculated = config.checksum_global()
    print("global:", status_text(stored, calculated))
    ok = ok and stored == calculated
    stored = config.stored_calibration_checksum()
    calculated = config.checksum_calibration()
    print("calibration:", status_text(stored, calculated))
    ok = ok and stored == calculated
    for slot in config.present_slots():
        stored = config.stored_motor_checksum(slot)
        calculated = config.checksum_motor(slot)
        slot_ok = stored == calculated
        ok = ok and slot_ok
        print(f"motor.{slot} {config.get_motor(slot, 'name')!r}:", status_text(stored, calculated))
    return 0 if ok else 1


def command_edit(args: argparse.Namespace) -> int:
    config = Ws22Config.read(args.input)
    touched_slots = apply_edits(args, config)
    touched_slots = recompute_after_edits(
        config,
        touched_slots,
        args.recompute_all_slot_checksums,
        args.no_recompute_touched_slot_checksums,
    )

    config.write(args.output)
    print(f"wrote {args.output}")
    print("global checksum:", status_text(config.stored_global_checksum(), config.checksum_global()))
    print(
        "calibration checksum:",
        status_text(config.stored_calibration_checksum(), config.checksum_calibration()),
    )
    for slot in sorted(touched_slots):
        print(
            f"motor.{slot} checksum:",
            status_text(config.stored_motor_checksum(slot), config.checksum_motor(slot)),
        )
    return 0


def command_create(args: argparse.Namespace) -> int:
    config = Ws22Config.read(args.template)
    touched_slots = apply_edits(args, config)
    touched_slots = recompute_after_edits(
        config,
        touched_slots,
        args.recompute_all_slot_checksums,
        args.no_recompute_touched_slot_checksums,
    )
    config.write(args.output)
    print(f"created {args.output} from template {args.template}")
    print("global checksum:", status_text(config.stored_global_checksum(), config.checksum_global()))
    print(
        "calibration checksum:",
        status_text(config.stored_calibration_checksum(), config.checksum_calibration()),
    )
    for slot in sorted(touched_slots):
        print(
            f"motor.{slot} checksum:",
            status_text(config.stored_motor_checksum(slot), config.checksum_motor(slot)),
        )
    return 0


def command_calibrate(args: argparse.Namespace) -> int:
    config = Ws22Config.read(args.input)
    points = [parse_calibration_point(point) for point in args.point]
    old_scale, old_offset, new_scale, new_offset = solve_calibration(
        config, args.channel, points, args.mode, args.base
    )

    scale_name = f"{args.channel}_scale"
    offset_name = f"{args.channel}_offset"
    print(f"channel: {args.channel}")
    print(f"base: {calibration_base(config, args.channel, args.base):.9g}")
    print(f"old {scale_name}: {old_scale:.9g}")
    print(f"old {offset_name}: {old_offset:.9g}")
    print(f"new {scale_name}: {new_scale:.9g}")
    print(f"new {offset_name}: {new_offset:.9g}")
    print("edit flags:")
    print(f"  --set {scale_name}={new_scale:.9g} --set {offset_name}={new_offset:.9g}")

    if args.output is not None:
        config.set_global(scale_name, new_scale)
        config.set_global(offset_name, new_offset)
        config.recompute_global_checksum()
        config.recompute_calibration_checksum()
        config.write(args.output)
        print(f"wrote {args.output}")
        print("global checksum:", status_text(config.stored_global_checksum(), config.checksum_global()))
        print(
            "calibration checksum:",
            status_text(config.stored_calibration_checksum(), config.checksum_calibration()),
        )

    return 0


def comparable_values(config: Ws22Config) -> Iterable[tuple[str, Any]]:
    for field in GLOBAL_FIELDS:
        yield (field.name, config.read_at(field.offset, field.kind))
    for slot in config.present_slots():
        for field in MOTOR_FIELDS:
            yield (f"motor.{slot}.{field.name}", config.get_motor(slot, field.name))
        for field in MOTOR_VIRTUAL_FIELDS:
            yield (f"motor.{slot}.{field.name}", config.get_motor(slot, field.name))


def values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, float) or isinstance(right, float):
        try:
            return math.isclose(float(left), float(right), rel_tol=1e-6, abs_tol=1e-9)
        except (TypeError, ValueError):
            return False
    return left == right


def command_diff(args: argparse.Namespace) -> int:
    left = Ws22Config.read(args.left)
    right = Ws22Config.read(args.right)
    left_values = dict(comparable_values(left))
    right_values = dict(comparable_values(right))
    names = sorted(set(left_values) | set(right_values))
    found = False
    for name in names:
        lv = left_values.get(name)
        rv = right_values.get(name)
        if not values_equal(lv, rv):
            found = True
            print(f"{name}: {format_value(name, lv)} != {format_value(name, rv)}")
    return 1 if found else 0


def command_fields(args: argparse.Namespace) -> int:
    print("global fields:")
    for field in GLOBAL_FIELDS:
        print(f"  {field.name:<32} {field.kind:<7} {field.help}")
    print()
    print("motor fields use motor.<slot|active>.<field>=value:")
    for field in MOTOR_FIELDS + MOTOR_VIRTUAL_FIELDS:
        print(f"  {field.name:<32} {field.kind:<7} {field.help}")
    return 0


def add_edit_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--set",
        action="append",
        metavar="FIELD=VALUE",
        help="set field, e.g. max_bus_voltage_v=150 or motor.active.line_inductance_h=6.48e-5",
    )
    parser.add_argument(
        "--copy-motor",
        action="append",
        metavar="FROM:TO",
        help="copy a complete motor slot before applying --set edits, e.g. 0:2 or active:3",
    )
    parser.add_argument(
        "--recompute-all-slot-checksums",
        action="store_true",
        help="rewrite every complete motor-slot checksum, not just edited slots",
    )
    parser.add_argument(
        "--no-recompute-touched-slot-checksums",
        action="store_true",
        help="preserve motor-slot checksums even for edited slots",
    )


def long_help() -> str:
    channels = ", ".join(CALIBRATION_CHANNELS)
    return textwrap.dedent(
        f"""

        File behavior:
          The tool reads WaveSculptor 22 .cfg files with an 1880-byte config body.
          If the source file has a save-tool trailer after the body, dump reports it,
          but edit/create/calibrate always write a canonical 1880-byte file.

        Command syntax:
          dump INPUT [--json]
              Decode INPUT. Text output shows checksums, globals, and each motor slot.
              --json emits the full decoded structure as JSON.

          check INPUT
              Validate the global, calibration, and all motor-slot checksums.

          diff LEFT RIGHT
              Compare decoded known fields. Output is FIELD: LEFT != RIGHT.
              Exit status is 0 for no decoded differences, 1 for differences.

          fields
              List editable global fields and motor fields.

          edit INPUT OUTPUT [EDIT-OPTION...]
              Copy INPUT to OUTPUT while applying edits. Global and calibration
              checksums are always recomputed.

          create TEMPLATE OUTPUT [EDIT-OPTION...]
              Start from TEMPLATE and write OUTPUT. Use this for creating a fresh
              controller config from a known-good template.

          calibrate INPUT CHANNEL --point REPORTED=TRUE [--point REPORTED=TRUE ...]
                    [--mode fit|scale|offset] [--base BASE] [--output OUTPUT]
              Solve a scale/offset calibration channel from observed telemetry.
              Without --output, prints the --set flags you would apply.
              Channels: {channels}

        Edit options:
          --set FIELD=VALUE
              Set one field. Repeat --set for multiple fields.

              Global field syntax:
                field_name=value

              Motor field syntax:
                motor.<slot|active>.field_name=value

              Motor slot is 0..9 or active. Examples:
                motor.active.name=Left Motor
                motor.2.encoder_count=1024
                motor.1.thermistor_ro_ohm=47000

              Value syntax:
                f32 fields: decimal or scientific notation, e.g. 134.4 or 6.48e-5
                u16/u32 fields: decimal or 0x-prefixed hex, e.g. 1024 or 0x420
                str20 fields: ASCII text up to 19 bytes
                bytes fields: hex bytes, with optional spaces

          --copy-motor FROM:TO
              Copy an entire motor slot before applying --set edits.
              FROM and TO are 0..9 or active. Repeat to copy multiple slots.

          --recompute-all-slot-checksums
              Recompute every motor slot checksum, not only touched slots.

          --no-recompute-touched-slot-checksums
              Leave touched motor-slot checksums unchanged. This is mainly useful
              for format investigation.

        Thermistor fields:
          motor.<slot>.thermistor_ro_ohm is the GUI-style Ro value in ohms.
          motor.<slot>.thermistor_raw_coefficient is the stored file coefficient:
            A = Ro * exp(-Beta / 298.15 K)
          Setting thermistor_beta preserves thermistor_ro_ohm and rewrites A.

        Examples:
          ws22_config.py dump left.cfg
          ws22_config.py dump left.cfg --json
          ws22_config.py check left.cfg
          ws22_config.py diff old.cfg new.cfg
          ws22_config.py fields
          ws22_config.py edit in.cfg out.cfg --set max_bus_voltage_v=160
          ws22_config.py edit in.cfg out.cfg --copy-motor 0:2 --set active_motor=2
          ws22_config.py create ws22defaults.cfg new.cfg --set base_address=0x400
          ws22_config.py calibrate in.cfg bus_voltage --mode scale --point 120.1=121.0 --output out.cfg
        """
    ).strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=long_help(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    dump = subparsers.add_parser("dump", help="decode a config file")
    dump.add_argument("input", type=Path)
    dump.add_argument("--json", action="store_true", help="emit JSON")
    dump.set_defaults(func=command_dump)

    check = subparsers.add_parser("check", help="validate known checksums")
    check.add_argument("input", type=Path)
    check.set_defaults(func=command_check)

    edit = subparsers.add_parser("edit", help="copy a config file while applying field edits")
    edit.add_argument("input", type=Path)
    edit.add_argument("output", type=Path)
    add_edit_options(edit)
    edit.set_defaults(func=command_edit)

    create = subparsers.add_parser("create", help="create a config from a known-good template")
    create.add_argument("template", type=Path, help="template cfg to preserve calibration/unknown bytes from")
    create.add_argument("output", type=Path)
    add_edit_options(create)
    create.set_defaults(func=command_create)

    calibrate = subparsers.add_parser(
        "calibrate",
        help="solve one scale/offset calibration channel from reported-vs-true points",
    )
    calibrate.add_argument("input", type=Path)
    calibrate.add_argument("channel", choices=CALIBRATION_CHANNELS)
    calibrate.add_argument(
        "--point",
        action="append",
        required=True,
        metavar="REPORTED=TRUE",
        help="one measurement pair in physical units; repeat for multi-point fit",
    )
    calibrate.add_argument(
        "--mode",
        choices=["fit", "scale", "offset"],
        default="fit",
        help="fit scale+offset with >=2 points, or adjust only scale/offset",
    )
    calibrate.add_argument(
        "--base",
        type=float,
        help="override normalization base; default is 15V for rails, base voltage/current, or 128C",
    )
    calibrate.add_argument("--output", type=Path, help="write an updated cfg with the solved fields")
    calibrate.set_defaults(func=command_calibrate)

    diff = subparsers.add_parser("diff", help="compare known fields in two config files")
    diff.add_argument("left", type=Path)
    diff.add_argument("right", type=Path)
    diff.set_defaults(func=command_diff)

    fields = subparsers.add_parser("fields", help="list editable field names")
    fields.set_defaults(func=command_fields)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
