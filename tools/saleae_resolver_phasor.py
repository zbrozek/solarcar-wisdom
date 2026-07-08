#!/usr/bin/env python3
"""Fit motor BEMF phase against AD2S1210 ABZ/NM output from Saleae binaries.

This is meant for the "spin the motor unpowered and measure two phase voltages
plus resolver encoder emulation" test. It decodes the quadrature A/B signals,
uses the NM/Z marker as resolver zero, and fits:

    v_bemf = dc + s*sin(2*pi*m*r) + c*cos(2*pi*m*r)

where r is resolver cycles since the marker and m is motor electrical cycles per
resolver cycle. For a 14 pole-pair motor with a two-lobe resolver, m = 14 / 2 =
7. The fitted phase is reported in electrical degrees and as a resolver-cycle
fraction modulo one equivalent branch (1/m). A second fit scales the sine/cosine
terms by measured mechanical speed from adjacent marker intervals, giving a
BEMF speed constant even during a modest coast-down.

Saleae binary inputs are one file per exported channel:
  * --phase-pos-bin: positive phase analog channel
  * --phase-neg-bin: negative phase analog channel
  * --a-bin, --b-bin, --z-bin/--nm-bin: digital ABZ/NM channels

The reported WS22 offset applies a fixed BEMF-to-config convention. The default
is the convention inferred from the existing PhasorSense config files:

    WS22 hall_angle branch = measured line-line BEMF branch - 90 electrical deg
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np


@dataclass(frozen=True)
class AnalogBinaryWaveform:
    path: Path
    begin_time: float
    trigger_time: float
    sample_rate: float
    downsample: int
    num_samples: int
    sample_offset: int
    samples: np.memmap

    @property
    def sample_period(self) -> float:
        downsample = self.downsample if self.downsample > 0 else 1
        return downsample / self.sample_rate


@dataclass(frozen=True)
class DigitalBinaryChunk:
    initial_state: int
    sample_rate: float
    begin_time: float
    end_time: float
    transition_times: np.ndarray


@dataclass(frozen=True)
class DigitalBinaryChannel:
    path: Path
    chunks: list[DigitalBinaryChunk]


@dataclass
class ScanStats:
    rows_seen: int = 0
    rows_used: int = 0
    bad_rows: int = 0
    illegal_transitions: int = 0
    legal_transitions: int = 0
    marker_edges: int = 0
    first_time: float | None = None
    last_time: float | None = None
    first_count: int | None = None
    last_count: int | None = None
    marker_counts: list[int] | None = None
    marker_times: list[float] | None = None


@dataclass(frozen=True)
class QuadratureTrace:
    event_times: np.ndarray
    counts: np.ndarray
    marker_times: np.ndarray
    marker_counts: np.ndarray
    stats: ScanStats


@dataclass
class FitAccum:
    n: int = 0
    sum_y: float = 0.0
    sum_y2: float = 0.0
    xtx: np.ndarray | None = None
    xty: np.ndarray | None = None
    min_time: float | None = None
    max_time: float | None = None

    def __post_init__(self) -> None:
        if self.xtx is None:
            self.xtx = np.zeros((3, 3), dtype=float)
        if self.xty is None:
            self.xty = np.zeros(3, dtype=float)


@dataclass
class MultiHarmonicAccum:
    frequencies: list[float]
    n: int = 0
    sum_y: float = 0.0
    sum_y2: float = 0.0
    xtx: np.ndarray | None = None
    xty: np.ndarray | None = None

    def __post_init__(self) -> None:
        size = 1 + 2 * len(self.frequencies)
        if self.xtx is None:
            self.xtx = np.zeros((size, size), dtype=float)
        if self.xty is None:
            self.xty = np.zeros(size, dtype=float)


@dataclass
class SpeedStats:
    samples: int = 0
    sum_signed: float = 0.0
    sum_abs: float = 0.0
    min_signed: float | None = None
    max_signed: float | None = None
    min_abs: float | None = None
    max_abs: float | None = None

    @property
    def mean_signed(self) -> float | None:
        return self.sum_signed / self.samples if self.samples else None

    @property
    def mean_abs(self) -> float | None:
        return self.sum_abs / self.samples if self.samples else None


@dataclass
class BemfFits:
    waveform: FitAccum
    per_speed: FitAccum
    speed: SpeedStats
    harmonics: MultiHarmonicAccum | None = None


def read_saleae_common_header(f) -> tuple[int, int]:
    identifier = f.read(8)
    if identifier != b"<SALEAE>":
        raise ValueError("not a Saleae binary export: missing <SALEAE> header")
    return struct.unpack("<ii", f.read(8))


def read_analog_binary(path: Path, waveform_index: int = 0) -> AnalogBinaryWaveform:
    with path.open("rb") as f:
        version, data_type = read_saleae_common_header(f)
        if version != 1:
            raise ValueError(f"{path}: unsupported Saleae analog version {version}")
        if data_type != 1:
            raise ValueError(f"{path}: expected analog binary type 1, got {data_type}")
        (waveform_count,) = struct.unpack("<Q", f.read(8))
        if waveform_index < 0 or waveform_index >= waveform_count:
            raise ValueError(
                f"{path}: waveform index {waveform_index} outside 0..{waveform_count - 1}"
            )

        selected: tuple[float, float, float, int, int, int] | None = None
        for i in range(waveform_count):
            begin_time, trigger_time, sample_rate = struct.unpack("<ddd", f.read(24))
            (downsample,) = struct.unpack("<q", f.read(8))
            (num_samples,) = struct.unpack("<Q", f.read(8))
            sample_offset = f.tell()
            if i == waveform_index:
                selected = (
                    begin_time,
                    trigger_time,
                    sample_rate,
                    int(downsample),
                    int(num_samples),
                    sample_offset,
                )
            f.seek(num_samples * 4, 1)

    assert selected is not None
    begin_time, trigger_time, sample_rate, downsample, num_samples, sample_offset = selected
    samples = np.memmap(
        path,
        dtype="<f4",
        mode="r",
        offset=sample_offset,
        shape=(num_samples,),
    )
    return AnalogBinaryWaveform(
        path=path,
        begin_time=begin_time,
        trigger_time=trigger_time,
        sample_rate=sample_rate,
        downsample=downsample,
        num_samples=num_samples,
        sample_offset=sample_offset,
        samples=samples,
    )


def read_digital_binary(path: Path) -> DigitalBinaryChannel:
    chunks = []
    with path.open("rb") as f:
        version, data_type = read_saleae_common_header(f)
        if version != 1:
            raise ValueError(f"{path}: unsupported Saleae digital version {version}")
        if data_type != 0:
            raise ValueError(f"{path}: expected digital binary type 0, got {data_type}")
        (chunk_count,) = struct.unpack("<Q", f.read(8))
        for _ in range(chunk_count):
            (initial_state,) = struct.unpack("<I", f.read(4))
            sample_rate, begin_time, end_time = struct.unpack("<ddd", f.read(24))
            (num_transitions,) = struct.unpack("<Q", f.read(8))
            raw = f.read(num_transitions * 8)
            transitions = np.frombuffer(raw, dtype="<f8").copy()
            chunks.append(
                DigitalBinaryChunk(
                    initial_state=int(initial_state != 0),
                    sample_rate=sample_rate,
                    begin_time=begin_time,
                    end_time=end_time,
                    transition_times=transitions,
                )
            )
    return DigitalBinaryChannel(path=path, chunks=chunks)


def quadrature_state(a: int, b: int, swap_ab: bool) -> int:
    if swap_ab:
        a, b = b, a
    return (a << 1) | b


def quadrature_step(prev: int, curr: int) -> int | None:
    if prev == curr:
        return 0
    order = {0b00: 0, 0b01: 1, 0b11: 2, 0b10: 3}
    delta = (order[curr] - order[prev]) % 4
    if delta == 1:
        return 1
    if delta == 3:
        return -1
    return None


def marker_edge(prev: int | None, curr: int, edge: str) -> bool:
    if prev is None:
        return False
    if edge == "rising":
        return prev == 0 and curr == 1
    if edge == "falling":
        return prev == 1 and curr == 0
    return prev != curr


def row_in_time_range(t: float, start_time: float | None, end_time: float | None) -> bool:
    if start_time is not None and t < start_time:
        return False
    if end_time is not None and t > end_time:
        return False
    return True


def require_single_chunk(channel: DigitalBinaryChannel, label: str) -> DigitalBinaryChunk:
    if len(channel.chunks) != 1:
        raise ValueError(
            f"{label}: expected one digital chunk, found {len(channel.chunks)}"
        )
    return channel.chunks[0]


def build_quadrature_trace(
    a_channel: DigitalBinaryChannel,
    b_channel: DigitalBinaryChannel,
    marker_channel: DigitalBinaryChannel,
    marker_edge_name: str,
    quad_sign: int,
    swap_ab: bool,
    start_time: float | None,
    end_time: float | None,
) -> QuadratureTrace:
    a_chunk = require_single_chunk(a_channel, "A")
    b_chunk = require_single_chunk(b_channel, "B")
    marker_chunk = require_single_chunk(marker_channel, "marker")

    state_a = a_chunk.initial_state
    state_b = b_chunk.initial_state
    state_marker = marker_chunk.initial_state
    prev_quad = quadrature_state(state_a, state_b, swap_ab)
    prev_marker = state_marker
    count = 0

    events: list[tuple[float, int]] = []
    events.extend((float(t), 0) for t in a_chunk.transition_times)
    events.extend((float(t), 1) for t in b_chunk.transition_times)
    events.extend((float(t), 2) for t in marker_chunk.transition_times)
    events.sort(key=lambda item: item[0])

    trace_times = [min(a_chunk.begin_time, b_chunk.begin_time, marker_chunk.begin_time)]
    trace_counts = [count]
    marker_times: list[float] = []
    marker_counts: list[int] = []
    stats = ScanStats(marker_counts=marker_counts, marker_times=marker_times)
    stats.rows_seen = len(events)

    i = 0
    while i < len(events):
        t = events[i][0]
        changed = {events[i][1]}
        i += 1
        while i < len(events) and events[i][0] == t:
            changed.add(events[i][1])
            i += 1

        if 0 in changed:
            state_a ^= 1
        if 1 in changed:
            state_b ^= 1
        if 2 in changed:
            state_marker ^= 1

        curr_quad = quadrature_state(state_a, state_b, swap_ab)
        step = quadrature_step(prev_quad, curr_quad)
        if step is None:
            stats.illegal_transitions += 1
        else:
            count += step * quad_sign
            if step != 0:
                stats.legal_transitions += 1

        if marker_edge(prev_marker, state_marker, marker_edge_name):
            stats.marker_edges += 1
            marker_times.append(t)
            marker_counts.append(count)

        prev_quad = curr_quad
        prev_marker = state_marker
        trace_times.append(t)
        trace_counts.append(count)

        if row_in_time_range(t, start_time, end_time):
            stats.rows_used += len(changed)
            if stats.first_time is None:
                stats.first_time = t
                stats.first_count = count
            stats.last_time = t
            stats.last_count = count

    if stats.first_time is None:
        stats.first_time = trace_times[0]
        stats.first_count = 0
    if stats.last_time is None:
        stats.last_time = trace_times[-1]
        stats.last_count = count

    return QuadratureTrace(
        event_times=np.array(trace_times, dtype=float),
        counts=np.array(trace_counts, dtype=float),
        marker_times=np.array(marker_times, dtype=float),
        marker_counts=np.array(marker_counts, dtype=float),
        stats=stats,
    )


def marker_period_from_scan(stats: ScanStats) -> tuple[float, dict[str, float]]:
    assert stats.marker_counts is not None
    if len(stats.marker_counts) < 2:
        raise ValueError("need at least two marker edges to infer counts per resolver marker")

    deltas = [
        abs(stats.marker_counts[i] - stats.marker_counts[i - 1])
        for i in range(1, len(stats.marker_counts))
    ]
    deltas = [delta for delta in deltas if delta > 0]
    if not deltas:
        raise ValueError("marker edges did not span any quadrature counts")

    median = float(statistics.median(deltas))
    mean = float(statistics.fmean(deltas))
    min_delta = float(min(deltas))
    max_delta = float(max(deltas))
    return median, {
        "mean": mean,
        "median": median,
        "min": min_delta,
        "max": max_delta,
        "span": max_delta - min_delta,
        "count": float(len(deltas)),
    }


def add_fit_rows(accum: FitAccum, times: np.ndarray, values: np.ndarray, design: np.ndarray) -> None:
    if len(values) == 0:
        return
    accum.xtx += design.T @ design
    accum.xty += design.T @ values
    accum.sum_y += float(np.sum(values))
    accum.sum_y2 += float(values @ values)
    accum.n += int(len(values))
    min_time = float(times[0])
    max_time = float(times[-1])
    if accum.min_time is None or min_time < accum.min_time:
        accum.min_time = min_time
    if accum.max_time is None or max_time > accum.max_time:
        accum.max_time = max_time


def add_speed_values(stats: SpeedStats, omega_mech: np.ndarray) -> None:
    if len(omega_mech) == 0:
        return
    abs_omega = np.abs(omega_mech)
    stats.samples += int(len(omega_mech))
    stats.sum_signed += float(np.sum(omega_mech))
    stats.sum_abs += float(np.sum(abs_omega))
    min_signed = float(np.min(omega_mech))
    max_signed = float(np.max(omega_mech))
    min_abs = float(np.min(abs_omega))
    max_abs = float(np.max(abs_omega))
    if stats.min_signed is None or min_signed < stats.min_signed:
        stats.min_signed = min_signed
    if stats.max_signed is None or max_signed > stats.max_signed:
        stats.max_signed = max_signed
    if stats.min_abs is None or min_abs < stats.min_abs:
        stats.min_abs = min_abs
    if stats.max_abs is None or max_abs > stats.max_abs:
        stats.max_abs = max_abs


def add_harmonic_rows(
    accum: MultiHarmonicAccum,
    resolver_cycles_total: np.ndarray,
    values: np.ndarray,
) -> None:
    if len(values) == 0:
        return
    design = np.empty((len(values), 1 + 2 * len(accum.frequencies)), dtype=float)
    design[:, 0] = 1.0
    for i, freq in enumerate(accum.frequencies):
        angle = 2.0 * math.pi * freq * resolver_cycles_total
        design[:, 1 + 2 * i] = np.sin(angle)
        design[:, 2 + 2 * i] = np.cos(angle)
    accum.xtx += design.T @ design
    accum.xty += design.T @ values
    accum.sum_y += float(np.sum(values))
    accum.sum_y2 += float(values @ values)
    accum.n += int(len(values))


def validate_compatible_analog_binaries(
    phase_pos: AnalogBinaryWaveform,
    phase_neg: AnalogBinaryWaveform,
) -> None:
    if phase_pos.num_samples != phase_neg.num_samples:
        raise ValueError(
            "analog binary channels have different sample counts: "
            f"{phase_pos.num_samples} vs {phase_neg.num_samples}"
        )
    if not math.isclose(phase_pos.begin_time, phase_neg.begin_time, abs_tol=1e-12):
        raise ValueError(
            "analog binary channels have different begin times: "
            f"{phase_pos.begin_time} vs {phase_neg.begin_time}"
        )
    if not math.isclose(phase_pos.sample_period, phase_neg.sample_period, rel_tol=1e-12):
        raise ValueError(
            "analog binary channels have different sample periods: "
            f"{phase_pos.sample_period} vs {phase_neg.sample_period}"
        )


def fit_bemf_binary(
    phase_pos: AnalogBinaryWaveform,
    phase_neg: AnalogBinaryWaveform,
    trace: QuadratureTrace,
    start_time: float | None,
    end_time: float | None,
    counts_per_marker: float,
    electrical_per_resolver: float,
    resolver_cycles_per_mech: float,
    bemf_scale: float,
    bemf_offset: float,
    fit_every: int,
    harmonic_frequencies: Sequence[float],
) -> BemfFits:
    validate_compatible_analog_binaries(phase_pos, phase_neg)
    if len(trace.marker_times) < 1:
        raise ValueError("need at least one marker edge to fit BEMF phase")
    if len(trace.event_times) < 1:
        raise ValueError("digital quadrature trace is empty")

    waveform_accum = FitAccum()
    speed_accum = FitAccum()
    speed_stats = SpeedStats()
    harmonic_accum = (
        MultiHarmonicAccum(list(harmonic_frequencies))
        if harmonic_frequencies else None
    )

    sample_period = phase_pos.sample_period
    begin_time = phase_pos.begin_time
    n_samples = phase_pos.num_samples
    selected_per_chunk = 200_000
    raw_span = max(fit_every * selected_per_chunk, fit_every)

    for raw_start in range(0, n_samples, raw_span):
        raw_stop = min(n_samples, raw_start + raw_span)
        sample_indexes = np.arange(raw_start, raw_stop, fit_every, dtype=np.int64)
        if len(sample_indexes) == 0:
            continue

        times = begin_time + sample_indexes.astype(float) * sample_period
        valid = np.ones(len(times), dtype=bool)
        if start_time is not None:
            valid &= times >= start_time
        if end_time is not None:
            valid &= times <= end_time
        if not np.any(valid):
            continue

        sample_indexes = sample_indexes[valid]
        times = times[valid]
        event_indexes = np.searchsorted(trace.event_times, times, side="right") - 1
        marker_indexes = np.searchsorted(trace.marker_times, times, side="right") - 1
        valid = (event_indexes >= 0) & (marker_indexes >= 0)
        if not np.any(valid):
            continue

        sample_indexes = sample_indexes[valid]
        times = times[valid]
        event_indexes = event_indexes[valid]
        marker_indexes = marker_indexes[valid]

        values = (
            phase_pos.samples[sample_indexes].astype(np.float64)
            - phase_neg.samples[sample_indexes].astype(np.float64)
        )
        values = values * bemf_scale + bemf_offset

        counts = trace.counts[event_indexes]
        marker_counts = trace.marker_counts[marker_indexes]
        resolver_cycles = (counts - marker_counts) / counts_per_marker
        angle = 2.0 * math.pi * electrical_per_resolver * resolver_cycles

        design = np.column_stack(
            (
                np.ones(len(values), dtype=float),
                np.sin(angle),
                np.cos(angle),
            )
        )
        add_fit_rows(waveform_accum, times, values, design)

        if harmonic_accum is not None:
            add_harmonic_rows(harmonic_accum, counts / counts_per_marker, values)

        speed_valid = marker_indexes + 1 < len(trace.marker_times)
        if np.any(speed_valid):
            speed_marker_indexes = marker_indexes[speed_valid]
            speed_times = times[speed_valid]
            speed_values = values[speed_valid]
            speed_angle = angle[speed_valid]
            dt = (
                trace.marker_times[speed_marker_indexes + 1]
                - trace.marker_times[speed_marker_indexes]
            )
            dcount = (
                trace.marker_counts[speed_marker_indexes + 1]
                - trace.marker_counts[speed_marker_indexes]
            )
            good_speed = (dt > 0.0) & (dcount != 0.0)
            if np.any(good_speed):
                speed_times = speed_times[good_speed]
                speed_values = speed_values[good_speed]
                speed_angle = speed_angle[good_speed]
                omega_mech = (
                    (dcount[good_speed] / counts_per_marker)
                    * 2.0
                    * math.pi
                    / resolver_cycles_per_mech
                    / dt[good_speed]
                )
                speed_design = np.column_stack(
                    (
                        np.ones(len(speed_values), dtype=float),
                        omega_mech * np.sin(speed_angle),
                        omega_mech * np.cos(speed_angle),
                    )
                )
                add_fit_rows(speed_accum, speed_times, speed_values, speed_design)
                add_speed_values(speed_stats, omega_mech)

    return BemfFits(
        waveform=waveform_accum,
        per_speed=speed_accum,
        speed=speed_stats,
        harmonics=harmonic_accum,
    )


def wrap_to_period(value: float, period: float) -> float:
    return value % period


def wrap_to_half_period(value: float, period: float) -> float:
    return (value + 0.5 * period) % period - 0.5 * period


def fit_summary(
    fits: BemfFits,
    counts_per_marker: float,
    electrical_per_resolver: float,
    resolver_cycles_per_mech: float,
    voltage_kind: str,
    ws22_config_offset_deg: float,
    stats: ScanStats,
    period_stats: dict[str, float] | None,
    reference_offset: float | None,
) -> dict[str, object]:
    accum = fits.waveform
    if accum.n < 8:
        raise ValueError(f"not enough fit samples after marker/time filtering: {accum.n}")

    try:
        beta = np.linalg.solve(accum.xtx, accum.xty)
    except np.linalg.LinAlgError as exc:
        raise ValueError("singular fit matrix; capture may not span enough electrical phase") from exc
    dc, sin_coeff, cos_coeff = [float(x) for x in beta]
    amplitude = math.hypot(sin_coeff, cos_coeff)
    phase_rad = math.atan2(cos_coeff, sin_coeff)
    phase_electrical_cycles = phase_rad / (2.0 * math.pi)
    phase_electrical_degrees = math.degrees(phase_rad)
    resolver_offset_cycles = phase_rad / (2.0 * math.pi * electrical_per_resolver)

    rss = accum.sum_y2 - float(beta @ accum.xty)
    rss = max(0.0, rss)
    rms_residual = math.sqrt(rss / accum.n)
    mean_y = accum.sum_y / accum.n
    total_var = max(0.0, accum.sum_y2 - accum.n * mean_y * mean_y)
    r_squared = 1.0 - rss / total_var if total_var > 0.0 else None

    branch_width = 1.0 / abs(electrical_per_resolver)
    branch_offset = wrap_to_period(resolver_offset_cycles, branch_width)
    motor_constant: dict[str, object] | None = None

    if fits.per_speed.n >= 8:
        try:
            speed_beta = np.linalg.solve(fits.per_speed.xtx, fits.per_speed.xty)
        except np.linalg.LinAlgError as exc:
            raise ValueError("singular speed-normalized fit matrix") from exc

        speed_dc, speed_sin_coeff, speed_cos_coeff = [float(x) for x in speed_beta]
        peak_measured_per_rad_s = math.hypot(speed_sin_coeff, speed_cos_coeff)
        speed_phase_rad = math.atan2(speed_cos_coeff, speed_sin_coeff)
        speed_rss = fits.per_speed.sum_y2 - float(speed_beta @ fits.per_speed.xty)
        speed_rss = max(0.0, speed_rss)
        speed_rms_residual = math.sqrt(speed_rss / fits.per_speed.n)
        speed_mean_y = fits.per_speed.sum_y / fits.per_speed.n
        speed_total_var = max(
            0.0,
            fits.per_speed.sum_y2 - fits.per_speed.n * speed_mean_y * speed_mean_y,
        )
        speed_r_squared = (
            1.0 - speed_rss / speed_total_var if speed_total_var > 0.0 else None
        )

        if voltage_kind == "line-line":
            phase_neutral_peak_per_rad_s = peak_measured_per_rad_s / math.sqrt(3.0)
            ws22_phase_neutral_rms_per_rad_s = peak_measured_per_rad_s / math.sqrt(6.0)
        elif voltage_kind == "phase-neutral":
            phase_neutral_peak_per_rad_s = peak_measured_per_rad_s
            ws22_phase_neutral_rms_per_rad_s = peak_measured_per_rad_s / math.sqrt(2.0)
        else:
            raise ValueError(f"unsupported voltage kind: {voltage_kind}")

        measured_rms_per_rad_s = peak_measured_per_rad_s / math.sqrt(2.0)
        rpm_per_v_measured_rms = (
            60.0 / (2.0 * math.pi * measured_rms_per_rad_s)
            if measured_rms_per_rad_s > 0.0 else None
        )

        motor_constant = {
            "voltage_kind": voltage_kind,
            "samples": fits.per_speed.n,
            "dc_v": speed_dc,
            "peak_measured_v_per_mech_rad_s": peak_measured_per_rad_s,
            "rms_measured_v_per_mech_rad_s": measured_rms_per_rad_s,
            "phase_neutral_peak_v_per_mech_rad_s": phase_neutral_peak_per_rad_s,
            "ws22_phase_neutral_rms_v_per_mech_rad_s": ws22_phase_neutral_rms_per_rad_s,
            "rpm_per_v_measured_rms": rpm_per_v_measured_rms,
            "phase_electrical_degrees": math.degrees(speed_phase_rad),
            "rms_residual_v": speed_rms_residual,
            "r_squared": speed_r_squared,
            "sin_coeff_v_per_mech_rad_s": speed_sin_coeff,
            "cos_coeff_v_per_mech_rad_s": speed_cos_coeff,
            "resolver_cycles_per_mech": resolver_cycles_per_mech,
            "omega_mech_rad_s_mean_signed": fits.speed.mean_signed,
            "omega_mech_rad_s_mean_abs": fits.speed.mean_abs,
            "omega_mech_rad_s_min_signed": fits.speed.min_signed,
            "omega_mech_rad_s_max_signed": fits.speed.max_signed,
            "omega_mech_rad_s_min_abs": fits.speed.min_abs,
            "omega_mech_rad_s_max_abs": fits.speed.max_abs,
            "rpm_mean_abs": (
                fits.speed.mean_abs * 60.0 / (2.0 * math.pi)
                if fits.speed.mean_abs is not None else None
            ),
            "rpm_min_abs": (
                fits.speed.min_abs * 60.0 / (2.0 * math.pi)
                if fits.speed.min_abs is not None else None
            ),
            "rpm_max_abs": (
                fits.speed.max_abs * 60.0 / (2.0 * math.pi)
                if fits.speed.max_abs is not None else None
            ),
        }

    result: dict[str, object] = {
        "fit": {
            "samples": accum.n,
            "time_start_s": accum.min_time,
            "time_end_s": accum.max_time,
            "dc_v": dc,
            "amplitude_v": amplitude,
            "rms_residual_v": rms_residual,
            "r_squared": r_squared,
            "sin_coeff_v": sin_coeff,
            "cos_coeff_v": cos_coeff,
        },
        "phase": {
            "electrical_per_resolver": electrical_per_resolver,
            "phase_electrical_cycles": phase_electrical_cycles,
            "phase_electrical_degrees": phase_electrical_degrees,
            "resolver_offset_cycles_signed": resolver_offset_cycles,
            "resolver_branch_width_cycles": branch_width,
            "resolver_offset_cycles_mod_branch": branch_offset,
        },
        "motor_constant": motor_constant,
        "quadrature": {
            "rows_seen": stats.rows_seen,
            "rows_used": stats.rows_used,
            "bad_rows": stats.bad_rows,
            "legal_transitions": stats.legal_transitions,
            "illegal_transitions": stats.illegal_transitions,
            "marker_edges": stats.marker_edges,
            "first_time_s": stats.first_time,
            "last_time_s": stats.last_time,
            "first_count": stats.first_count,
            "last_count": stats.last_count,
            "counts_per_marker": counts_per_marker,
            "period_stats": period_stats,
        },
    }

    candidates: list[float] = []
    rounded_branches = round(abs(electrical_per_resolver))
    if abs(rounded_branches - abs(electrical_per_resolver)) < 1e-9:
        candidates = [branch_offset + k * branch_width for k in range(int(rounded_branches))]
        result["phase"]["equivalent_offsets_0_to_1"] = candidates

    ws22_shift_cycles = ws22_config_offset_deg / (360.0 * abs(electrical_per_resolver))
    ws22_branch = wrap_to_period(branch_offset + ws22_shift_cycles, branch_width)
    ws22_candidates: list[float] = []
    if abs(rounded_branches - abs(electrical_per_resolver)) < 1e-9:
        ws22_candidates = [
            ws22_branch + k * branch_width for k in range(int(rounded_branches))
        ]
    else:
        ws22_candidates = [ws22_branch]

    result["ws22_config_offset"] = {
        "bemf_to_config_electrical_degrees": ws22_config_offset_deg,
        "bemf_to_config_resolver_cycles": ws22_shift_cycles,
        "branch_width_cycles": branch_width,
        "config_branch_cycles": ws22_branch,
        "equivalent_offsets_0_to_1": ws22_candidates,
        "hall_angle_1_to_7": ws22_branch,
    }

    if reference_offset is not None:
        reference_branch = wrap_to_period(reference_offset, branch_width)
        delta = wrap_to_half_period(branch_offset - reference_branch, branch_width)
        result["reference_offset"] = {
            "reference_cycles": reference_offset,
            "reference_mod_branch": reference_branch,
            "delta_cycles_mod_branch": delta,
            "delta_electrical_degrees": delta * abs(electrical_per_resolver) * 360.0,
        }

    if fits.harmonics is not None and fits.harmonics.n >= 8:
        try:
            harmonic_beta = np.linalg.solve(fits.harmonics.xtx, fits.harmonics.xty)
        except np.linalg.LinAlgError as exc:
            raise ValueError("singular harmonic fit matrix") from exc

        harmonic_rss = fits.harmonics.sum_y2 - float(harmonic_beta @ fits.harmonics.xty)
        harmonic_rss = max(0.0, harmonic_rss)
        harmonic_rms_residual = math.sqrt(harmonic_rss / fits.harmonics.n)
        harmonic_mean_y = fits.harmonics.sum_y / fits.harmonics.n
        harmonic_total_var = max(
            0.0,
            fits.harmonics.sum_y2 - fits.harmonics.n * harmonic_mean_y * harmonic_mean_y,
        )
        harmonic_r_squared = (
            1.0 - harmonic_rss / harmonic_total_var
            if harmonic_total_var > 0.0 else None
        )
        components = []
        for i, freq in enumerate(fits.harmonics.frequencies):
            sin_coeff = float(harmonic_beta[1 + 2 * i])
            cos_coeff = float(harmonic_beta[2 + 2 * i])
            components.append(
                {
                    "frequency_cycles_per_resolver": freq,
                    "amplitude_v": math.hypot(sin_coeff, cos_coeff),
                    "phase_degrees": math.degrees(math.atan2(cos_coeff, sin_coeff)),
                    "sin_coeff_v": sin_coeff,
                    "cos_coeff_v": cos_coeff,
                }
            )

        result["harmonic_fit"] = {
            "samples": fits.harmonics.n,
            "dc_v": float(harmonic_beta[0]),
            "rms_residual_v": harmonic_rms_residual,
            "r_squared": harmonic_r_squared,
            "components": components,
        }

    return result


def print_text_summary(result: dict[str, object]) -> None:
    fit = result["fit"]
    phase = result["phase"]
    quad = result["quadrature"]

    print("Fit")
    print(f"  samples:          {fit['samples']}")
    print(f"  time window:      {fit['time_start_s']:.9g} to {fit['time_end_s']:.9g} s")
    print(f"  dc:               {fit['dc_v']:.6g} V")
    print(f"  amplitude:        {fit['amplitude_v']:.6g} V")
    print(f"  rms residual:     {fit['rms_residual_v']:.6g} V")
    if fit["r_squared"] is not None:
        print(f"  r^2:              {fit['r_squared']:.6f}")

    motor_constant = result.get("motor_constant")
    print()
    print("Motor Constant")
    if motor_constant is None:
        print("  unavailable:      need at least two clean NM marker intervals")
    else:
        mc = motor_constant
        print(f"  voltage kind:     {mc['voltage_kind']}")
        print(f"  samples:          {mc['samples']}")
        print(f"  speed range:      {mc['rpm_min_abs']:.3f} to {mc['rpm_max_abs']:.3f} rpm abs")
        print(f"  mean speed:       {mc['rpm_mean_abs']:.3f} rpm abs")
        print(
            "  measured peak:    "
            f"{mc['peak_measured_v_per_mech_rad_s']:.6g} V/(mech rad/s)"
        )
        print(
            "  measured RMS:     "
            f"{mc['rms_measured_v_per_mech_rad_s']:.6g} V_RMS/(mech rad/s)"
        )
        print(
            "  phase-neutral pk: "
            f"{mc['phase_neutral_peak_v_per_mech_rad_s']:.6g} V/(mech rad/s)"
        )
        print(
            "  WS22 speed const: "
            f"{mc['ws22_phase_neutral_rms_v_per_mech_rad_s']:.6g} "
            "V_RMS phase-neutral/(mech rad/s)"
        )
        print(f"  Ke-fit residual:  {mc['rms_residual_v']:.6g} V")
        if mc["r_squared"] is not None:
            print(f"  Ke-fit r^2:       {mc['r_squared']:.6f}")

    print()
    print("Phase")
    print(f"  electrical/resolver:       {phase['electrical_per_resolver']:.9g}")
    print(f"  phase:                     {phase['phase_electrical_degrees']:.3f} electrical deg")
    print(f"  signed resolver offset:    {phase['resolver_offset_cycles_signed']:.9g} cycles")
    print(f"  branch width:              {phase['resolver_branch_width_cycles']:.9g} cycles")
    print(f"  offset modulo branch:      {phase['resolver_offset_cycles_mod_branch']:.9g} cycles")

    candidates = phase.get("equivalent_offsets_0_to_1")
    if candidates:
        print("  equivalent offsets [0,1): " + ", ".join(f"{x:.9g}" for x in candidates))

    if "reference_offset" in result:
        ref = result["reference_offset"]
        print()
        print("Reference Offset")
        print(f"  reference:        {ref['reference_cycles']:.9g} cycles")
        print(f"  branch delta:     {ref['delta_cycles_mod_branch']:.9g} cycles")
        print(f"  branch delta:     {ref['delta_electrical_degrees']:.3f} electrical deg")

    ws22 = result["ws22_config_offset"]
    print()
    print("WS22 Config Offset")
    print(
        "  BEMF -> config:   "
        f"{ws22['bemf_to_config_electrical_degrees']:.3f} electrical deg"
    )
    print(
        "  BEMF -> config:   "
        f"{ws22['bemf_to_config_resolver_cycles']:.9g} resolver cycles"
    )
    print(f"  hall_angle_1..7:  {ws22['hall_angle_1_to_7']:.9g}")
    equivalents = ws22.get("equivalent_offsets_0_to_1")
    if equivalents:
        print("  equivalents [0,1): " + ", ".join(f"{x:.9g}" for x in equivalents))

    if "harmonic_fit" in result:
        hf = result["harmonic_fit"]
        print()
        print("Harmonic Diagnostic")
        print(f"  samples:          {hf['samples']}")
        print(f"  rms residual:     {hf['rms_residual_v']:.6g} V")
        if hf["r_squared"] is not None:
            print(f"  r^2:              {hf['r_squared']:.6f}")
        print("  components:")
        for component in sorted(
            hf["components"],
            key=lambda c: c["amplitude_v"],
            reverse=True,
        ):
            print(
                "    "
                f"{component['frequency_cycles_per_resolver']:>8.4g} cyc/res: "
                f"{component['amplitude_v']:.6g} V, "
                f"phase {component['phase_degrees']:.2f} deg"
            )

    print()
    print("Quadrature")
    print(f"  rows used:        {quad['rows_used']} of {quad['rows_seen']}")
    print(f"  bad rows:         {quad['bad_rows']}")
    print(f"  transitions:      {quad['legal_transitions']}")
    print(f"  illegal trans.:   {quad['illegal_transitions']}")
    print(f"  marker edges:     {quad['marker_edges']}")
    print(f"  counts/marker:    {quad['counts_per_marker']:.9g}")
    if quad["period_stats"] is not None:
        ps = quad["period_stats"]
        print(
            "  period spread:    "
            f"min {ps['min']:.9g}, median {ps['median']:.9g}, max {ps['max']:.9g} counts"
        )

    print()
    print(
        "Note: the WS22 value above applies the default BEMF-to-config convention. "
        "Use --ws22-config-offset-deg if later testing shows this fixed convention "
        "has the opposite sign or a different phase."
    )


def parse_float_list(text: str) -> list[float]:
    values = []
    for part in text.split(","):
        stripped = part.strip()
        if not stripped:
            continue
        values.append(float(stripped))
    if not values:
        raise ValueError("frequency list must not be empty")
    return values


def print_analog_binary_summary(label: str, waveform: AnalogBinaryWaveform) -> None:
    end_time = waveform.begin_time + waveform.num_samples * waveform.sample_period
    print(f"{label}: {waveform.path}")
    print(f"  samples:      {waveform.num_samples}")
    print(f"  begin/end:    {waveform.begin_time:.12g} to {end_time:.12g} s")
    print(f"  sample rate:  {waveform.sample_rate:.12g} Hz")
    print(f"  downsample:   {waveform.downsample}")


def print_digital_binary_summary(label: str, channel: DigitalBinaryChannel) -> None:
    print(f"{label}: {channel.path}")
    print(f"  chunks:       {len(channel.chunks)}")
    for i, chunk in enumerate(channel.chunks):
        print(
            f"  chunk {i}:     initial {chunk.initial_state}, "
            f"{len(chunk.transition_times)} transitions, "
            f"{chunk.begin_time:.12g} to {chunk.end_time:.12g} s"
        )


def electrical_per_resolver_from_args(args: argparse.Namespace) -> float:
    value = (
        args.electrical_per_resolver
        if args.electrical_per_resolver is not None
        else args.motor_pole_pairs / args.resolver_cycles_per_mech
    )
    if value == 0.0:
        raise ValueError("electrical cycles per resolver cycle must be nonzero")
    return value


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fit line-line motor BEMF phase against Saleae binary ABZ/NM captures.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  saleae_resolver_phasor.py --phase-pos-bin PhaseA.bin --phase-neg-bin PhaseB.bin --a-bin A.bin --b-bin B.bin --z-bin NM.bin

  saleae_resolver_phasor.py --phase-pos-bin PhaseA.bin --phase-neg-bin PhaseB.bin --a-bin A.bin --b-bin B.bin --z-bin NM.bin --fit-every 10000 --diagnose-harmonics

  saleae_resolver_phasor.py --phase-pos-bin PhaseA.bin --phase-neg-bin PhaseB.bin --a-bin A.bin --b-bin B.bin --z-bin NM.bin --ws22-config-offset-deg -90
""",
    )
    parser.add_argument("--phase-pos-bin", type=Path, required=True, help="positive phase analog Saleae binary file")
    parser.add_argument("--phase-neg-bin", type=Path, required=True, help="negative phase analog Saleae binary file")
    parser.add_argument("--phase-pos-waveform-index", type=int, default=0, help="analog waveform index in --phase-pos-bin")
    parser.add_argument("--phase-neg-waveform-index", type=int, default=0, help="analog waveform index in --phase-neg-bin")
    parser.add_argument("--list-channels", "--list-columns", action="store_true", help="print binary channel metadata and exit")

    voltage = parser.add_argument_group("voltage input")
    voltage.add_argument(
        "--voltage-kind",
        choices=("line-line", "phase-neutral"),
        default="line-line",
        help="kind of BEMF voltage being fitted; two phase files normally produce line-line",
    )
    voltage.add_argument("--bemf-scale", type=float, default=1.0, help="multiply measured voltage by this scale")
    voltage.add_argument("--bemf-offset", type=float, default=0.0, help="add this voltage after scaling")

    digital = parser.add_argument_group("resolver digital input")
    digital.add_argument("--a-bin", type=Path, required=True, help="quadrature A digital Saleae binary file")
    digital.add_argument("--b-bin", type=Path, required=True, help="quadrature B digital Saleae binary file")
    digital.add_argument("--z-bin", "--nm-bin", "--marker-bin", dest="marker_bin", type=Path, required=True, help="marker/NM/Z digital Saleae binary file")
    digital.add_argument("--marker-edge", choices=("rising", "falling", "both"), default="rising")
    digital.add_argument("--quad-sign", type=int, choices=(-1, 1), default=1, help="multiply decoded quadrature count by this sign")
    digital.add_argument("--swap-ab", action="store_true", help="swap A and B before quadrature decoding")
    digital.add_argument("--counts-per-marker", type=float, help="override inferred quadrature counts per NM/Z marker")

    motor = parser.add_argument_group("motor/resolver geometry")
    motor.add_argument("--motor-pole-pairs", type=float, default=14.0)
    motor.add_argument("--resolver-cycles-per-mech", type=float, default=2.0)
    motor.add_argument(
        "--electrical-per-resolver",
        type=float,
        help="override motor_pole_pairs / resolver_cycles_per_mech",
    )

    fit = parser.add_argument_group("fit controls")
    fit.add_argument("--start-time", type=float, help="ignore samples before this Saleae time")
    fit.add_argument("--end-time", type=float, help="ignore samples after this Saleae time")
    fit.add_argument("--fit-every", type=int, default=1, help="use every Nth voltage sample in the fit")
    fit.add_argument("--reference-offset", type=float, help="report branch delta relative to this resolver-cycle offset")
    fit.add_argument(
        "--ws22-config-offset-deg",
        type=float,
        default=-90.0,
        help="electrical-degree offset added to measured BEMF branch for WS22 hall_angle_1..7",
    )
    fit.add_argument(
        "--diagnose-harmonics",
        action="store_true",
        help="fit extra angular harmonics and sidebands to explain residual structure",
    )
    fit.add_argument(
        "--harmonic-frequencies",
        default="0.5,1,6.5,7,7.5,14,21,35,49",
        help="comma-separated cycles/resolver for --diagnose-harmonics",
    )
    fit.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    if args.fit_every < 1:
        raise ValueError("--fit-every must be >= 1")
    if args.resolver_cycles_per_mech == 0:
        raise ValueError("--resolver-cycles-per-mech must be nonzero")
    if args.electrical_per_resolver == 0:
        raise ValueError("--electrical-per-resolver must be nonzero")
    if args.start_time is not None and args.end_time is not None and args.start_time >= args.end_time:
        raise ValueError("--start-time must be less than --end-time")
    if args.phase_pos_waveform_index < 0:
        raise ValueError("--phase-pos-waveform-index must be >= 0")
    if args.phase_neg_waveform_index < 0:
        raise ValueError("--phase-neg-waveform-index must be >= 0")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    try:
        validate_args(args)
        harmonic_frequencies = (
            parse_float_list(args.harmonic_frequencies)
            if args.diagnose_harmonics else []
        )
        electrical_per_resolver = electrical_per_resolver_from_args(args)

        phase_pos = read_analog_binary(args.phase_pos_bin, args.phase_pos_waveform_index)
        phase_neg = read_analog_binary(args.phase_neg_bin, args.phase_neg_waveform_index)
        a_channel = read_digital_binary(args.a_bin)
        b_channel = read_digital_binary(args.b_bin)
        marker_channel = read_digital_binary(args.marker_bin)

        if args.list_channels:
            print_analog_binary_summary("Positive phase analog", phase_pos)
            print()
            print_analog_binary_summary("Negative phase analog", phase_neg)
            print()
            print_digital_binary_summary("Quadrature A", a_channel)
            print()
            print_digital_binary_summary("Quadrature B", b_channel)
            print()
            print_digital_binary_summary("Marker", marker_channel)
            return 0

        trace = build_quadrature_trace(
            a_channel,
            b_channel,
            marker_channel,
            args.marker_edge,
            args.quad_sign,
            args.swap_ab,
            args.start_time,
            args.end_time,
        )

        period_stats = None
        if args.counts_per_marker is not None:
            counts_per_marker = abs(args.counts_per_marker)
        else:
            counts_per_marker, period_stats = marker_period_from_scan(trace.stats)
        if counts_per_marker <= 0:
            raise ValueError("counts per marker must be positive")

        accum = fit_bemf_binary(
            phase_pos,
            phase_neg,
            trace,
            args.start_time,
            args.end_time,
            counts_per_marker,
            electrical_per_resolver,
            args.resolver_cycles_per_mech,
            args.bemf_scale,
            args.bemf_offset,
            args.fit_every,
            harmonic_frequencies,
        )

        result = fit_summary(
            accum,
            counts_per_marker,
            electrical_per_resolver,
            args.resolver_cycles_per_mech,
            args.voltage_kind,
            args.ws22_config_offset_deg,
            trace.stats,
            period_stats,
            args.reference_offset,
        )

        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print_text_summary(result)
        return 0
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
