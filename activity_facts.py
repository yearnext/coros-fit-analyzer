"""Deterministic activity facts. No diagnosis, causal explanation or training plan.

Intervals use adjacent original records; gaps are never bridged. All durations
and averages expose their observed support. No interpolation is performed.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from statistics import median

POLICY = {
    "gap_min_seconds": 10, "gap_sampling_multiplier": 3,
    "gps_max_m_s": 15, "distance_max_m_s": 15,
    "altitude_max_m_s": 5, "hr_range_bpm": [25, 250],
    "hr_spike_bpm_s": 15, "hr_long_missing_seconds": 30,
    "elevation_hysteresis_m": 3, "minimum_segment_gain_m": 10,
    "grade_bins_percent": [3, 8, 15], "minimum_comparison_seconds": 120,
    "maximum_comparable_speed_difference_fraction": 0.1,
    "fatigue_pace_change_fraction": 0.1, "fatigue_other_change_fraction": 0.05,
    "corridor_radius_m": 30, "minimum_overlap_fraction": 0.8,
}
DYNAMICS = ("cadence", "fractional_cadence", "step_length", "stance_time",
            "ground_contact_time", "vertical_oscillation", "stance_time_balance", "vertical_ratio")


def num(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def stamp(value):
    try:
        value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def coordinate(row):
    lat, lon = num(row.get("latitude")), num(row.get("longitude"))
    return (lat, lon) if lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 180 else None


def haversine(a, b):
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    x = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(min(1, x)))


def blocked(reason):
    return {"status": "blocked", "reason": reason, "evidence_grade": None}


def metric(value, unit, support=None):
    return {"value": value, "unit": unit, "evidence_grade": "B" if value is not None else None,
            "support_seconds": support}


def prepare(records, summaries):
    times = [stamp(r.get("timestamp")) for r in records]
    deltas = [(b-a).total_seconds() for a,b in zip(times,times[1:]) if a is not None and b is not None and b > a]
    sampling = median(deltas) if deltas else None
    gap_limit = max(POLICY["gap_min_seconds"], (sampling or 0) * POLICY["gap_sampling_multiplier"])
    issues, edges = [], []
    bad = defaultdict(set)

    def issue(kind, i, j, fields, impact):
        issues.append({"kind": kind, "start_index": i, "end_index": j,
                       "start_time": records[i].get("timestamp"), "end_time": records[j].get("timestamp"),
                       "fields": fields, "impact": impact})
        for f in fields:
            bad[f].update((i,j))

    for i, row in enumerate(records):
        for f, limit in (("latitude",90),("longitude",180)):
            if num(row.get(f)) is not None and abs(row[f]) > limit:
                issue("invalid_coordinate", i, i, [f], "排除水平距离及轨迹对齐")
        if times[i] is None:
            issue("invalid_timestamp", i, i, ["timestamp"], "相邻区间不计入时间统计")
        hr = num(row.get("heart_rate"))
        if hr is not None and not 25 <= hr <= 250:
            issue("heart_rate_zero_or_out_of_range", i, i, ["heart_rate"], "排除心率统计")
        for f in ("distance", "speed", "cadence", "step_length"):
            v = num(row.get(f))
            if v is not None and v < 0:
                issue("negative_measurement", i, i, [f], "排除相关派生统计")
        for f in row:
            if isinstance(row[f], (float, int)) and not isinstance(row[f], bool) and num(row[f]) is None:
                issue("nonfinite_measurement", i, i, [f], "排除非有限值")
        if i == 0:
            continue
        a, b = records[i-1], row
        dt = (times[i]-times[i-1]).total_seconds() if times[i] and times[i-1] else None
        valid_time = dt is not None and 0 < dt <= gap_limit
        if dt is not None and (dt <= 0 or dt > gap_limit):
            issue("duplicate_timestamp" if dt == 0 else "backwards_timestamp" if dt < 0 else "timestamp_gap",
                  i-1, i, ["timestamp"], "断开区间，不跨缺口计算")
        dd = num(b.get("distance")) - num(a.get("distance")) if num(a.get("distance")) is not None and num(b.get("distance")) is not None else None
        dz = num(b.get("altitude")) - num(a.get("altitude")) if num(a.get("altitude")) is not None and num(b.get("altitude")) is not None else None
        ca, cb = coordinate(a), coordinate(b)
        horizontal = haversine(ca, cb) if ca and cb else None
        if valid_time:
            if dd is not None and (dd < 0 or dd / dt > POLICY["distance_max_m_s"]):
                issue("distance_reset_or_jump", i-1, i, ["distance"], "排除距离和配速，断开地形段")
            if horizontal is not None and horizontal / dt > POLICY["gps_max_m_s"]:
                issue("gps_jump", i-1, i, ["latitude", "longitude"], "排除水平距离坡度")
            if dz is not None and abs(dz) / dt > POLICY["altitude_max_m_s"]:
                issue("altitude_jump", i-1, i, ["altitude"], "断开坡段，排除该区间爬升")
            ha, hb = num(a.get("heart_rate")), num(b.get("heart_rate"))
            if ha is not None and hb is not None and abs(hb-ha)/dt > POLICY["hr_spike_bpm_s"]:
                issue("heart_rate_spike", i-1, i, ["heart_rate"], "排除尖峰两端心率")
            speed = num(a.get("speed"))
            if dd is not None and dd >= 0 and speed is not None and abs(dd/dt-speed) > max(2, speed * 0.5):
                issue("distance_speed_conflict", i-1, i, ["speed"], "速度来源不一致，使用质检后的距离增量")
        edges.append({"i": i-1, "j": i, "dt": dt, "time_ok": valid_time,
                      "distance": dd, "dz": dz, "horizontal": horizontal})

    # Only explicit FIT timer events establish pauses; low speed alone is not a pause.
    events = []
    for e in summaries.get("event", []):
        t = stamp(e.get("timestamp"))
        if e.get("event") == "timer" and t and e.get("event_type") in ("start", "stop", "stop_all", "stop_disable", "stop_disable_all"):
            events.append((t, e["event_type"]))
    events.sort()
    pauses, stop = [], None
    for t, kind in events:
        if kind != "start" and stop is None:
            stop = t
        elif kind == "start" and stop is not None:
            pauses.append((stop,t)); stop = None
    if stop is not None and times and times[-1] and times[-1] > stop:
        pauses.append((stop,times[-1]))
    for e in edges:
        e["active_dt"] = None
        if e["time_ok"]:
            a,b = times[e["i"]],times[e["j"]]
            paused = sum(max(0, (min(b,y)-max(a,x)).total_seconds()) for x,y in pauses)
            e["active_dt"] = max(0, e["dt"]-paused)
        for f,k in (("distance","distance"),("altitude","dz"),("latitude","horizontal"),("longitude","horizontal")):
            if e["i"] in bad[f] or e["j"] in bad[f]:
                e[k] = None
        if not e["time_ok"]:
            e["distance"] = e["dz"] = e["horizontal"] = None
        # A partially paused interval cannot locate movement inside its distance increment.
        e["usable"] = e["time_ok"] and e["active_dt"] == e["dt"]

    fields = sorted(set(DYNAMICS) | {"timestamp", "altitude", "distance", "speed", "heart_rate", "latitude", "longitude"} | {k for r in records for k in r})
    coverage = {}
    for f in fields:
        indexes = [i for i,r in enumerate(records) if r.get(f) is not None]
        valid = [i for i in indexes if i not in bad[f] and (stamp(records[i][f]) is not None if f == "timestamp" else num(records[i][f]) is not None)]
        steps = [(times[b]-times[a]).total_seconds() for a,b in zip(indexes,indexes[1:]) if times[a] and times[b] and times[b] > times[a]]
        coverage[f] = {"status": "recorded" if indexes else "not_recorded", "recorded_count": len(indexes),
                       "coverage_fraction": len(indexes)/len(records) if records else None,
                       "valid_count": len(valid), "median_sampling_seconds": median(steps) if steps else None,
                       "evidence_grade": "A" if indexes and len(valid)==len(indexes) else None}
    missing_start = None
    for i in range(len(records)+1):
        missing = i < len(records) and (num(records[i].get("heart_rate")) is None or i in bad["heart_rate"])
        if missing and missing_start is None:
            missing_start = i
        if not missing and missing_start is not None:
            end = i-1
            if times[end] and times[missing_start] and (times[end]-times[missing_start]).total_seconds() >= 30:
                issue("heart_rate_long_missing", missing_start, end, ["heart_rate"], "缺失区间不计入心率区间和漂移")
            missing_start = None
    return times, edges, bad, {"fields": coverage, "median_sampling_seconds": sampling, "gap_threshold_seconds": gap_limit,
                              "anomalies": issues, "pause_intervals": [{"start": a.isoformat(), "end": b.isoformat(), "seconds": (b-a).total_seconds()} for a,b in pauses],
                              "pause_source": "FIT timer events" if events else "unavailable"}


def aggregate(records, edges, bad):
    usable = [e for e in edges if e["usable"]]
    active = sum(e["active_dt"] for e in edges if e["active_dt"] is not None)
    distance_edges = [e for e in usable if e["distance"] is not None]
    distance = sum(e["distance"] for e in distance_edges) if distance_edges else None
    distance_time = sum(e["dt"] for e in distance_edges)
    result = {"active_seconds": metric(active if any(e["active_dt"] is not None for e in edges) else None, "s"),
              "distance_m": metric(distance, "m", distance_time),
              "pace_seconds_per_km": metric(distance_time/distance*1000 if distance and distance_time else None, "s/km", distance_time),
              "fields": {}}
    for f in ("heart_rate",)+DYNAMICS:
        values = [(num(records[e["i"]].get(f)),e["dt"]) for e in usable if e["i"] not in bad[f]]
        values = [(v,t) for v,t in values if v is not None]
        support = sum(t for _,t in values)
        observed_indexes = {e[k] for e in usable for k in ("i","j")}
        maxima = [num(records[i].get(f)) for i in observed_indexes if i not in bad[f] and num(records[i].get(f)) is not None]
        result["fields"][f] = {"mean": metric(sum(v*t for v,t in values)/support if support else None, "source FIT unit", support),
                                "max": max(maxima, default=None)}
    elevation_edges = [e for e in usable if e["dz"] is not None]
    up_time = sum(e["dt"] for e in elevation_edges if e["dz"] > 0)
    result["uphill_vam_m_h"] = metric(sum(max(0,e["dz"]) for e in elevation_edges)/up_time*3600 if up_time else None,
                                      "m/h", up_time)
    return result


def terrain(records, edges, bad):
    segments, runs, run = [], [], []
    for e in edges:
        if e["usable"] and e["dz"] is not None and e["distance"] is not None:
            if run and e["i"] != run[-1]["j"]:
                runs.append(run); run = []
            run.append(e)
        else:
            if run: runs.append(run)
            run = []
    if run: runs.append(run)
    ascent = descent = 0.0
    for run in runs:
        anchor = num(records[run[0]["i"]].get("altitude"))
        path = [anchor]
        for e in run:
            z = num(records[e["j"]].get("altitude"))
            if abs(z-anchor) >= 3: anchor = z
            path.append(anchor)
        start, direction = 0, 0
        def emit(end):
            change = path[end]-path[start]
            if abs(change) < 10: return
            selected = run[start:end]
            first,last = selected[0]["i"],selected[-1]["j"]
            summary = aggregate(records,selected,bad)
            distance = summary["distance_m"]["value"]
            horizontal = sum(e["horizontal"] for e in selected) if all(e["horizontal"] is not None for e in selected) else None
            duration = sum(e["dt"] for e in selected)
            segments.append({"kind": "climb" if change > 0 else "descent", "start_index": first, "end_index": last,
                "start_time": records[first].get("timestamp"), "end_time": records[last].get("timestamp"),
                "start_distance_m": num(records[first].get("distance")), "end_distance_m": num(records[last].get("distance")),
                "net_elevation_m": change, "ascent_m": sum(max(0,path[k+1]-path[k]) for k in range(start,end)),
                "descent_m": sum(max(0,path[k]-path[k+1]) for k in range(start,end)),
                "horizontal_distance_m": horizontal, "horizontal_grade_percent": change/horizontal*100 if horizontal and horizontal >= 5 else None,
                "track_grade_percent": change/distance*100 if distance and distance >= 5 else None,
                "vam_m_h": change/duration*3600 if change > 0 and duration else None,
                "grade_method": "GPS geodesic horizontal estimate; FIT distance denominator reported separately; hysteresis elevation",
                "evidence_grade": "B", **summary})
        for k in range(len(run)):
            delta = path[k+1]-path[k]
            ascent += max(0,delta); descent += max(0,-delta)
            sign = 1 if delta > 0 else -1 if delta < 0 else 0
            if sign and direction and sign != direction:
                emit(k); start = k
            if sign: direction = sign
        emit(len(run))
    splits = {}
    for width in (1000,5000):
        groups = defaultdict(list)
        origin = num(records[0].get("distance")) if records else None
        if origin is not None:
            for e in edges:
                d = num(records[e["i"]].get("distance"))
                if e["usable"] and e["distance"] is not None and d is not None and d >= origin:
                    groups[int((d-origin)//width)].append(e)
        splits[str(width)] = [{"bin": k, "nominal_start_m": k*width, "start_index": v[0]["i"], "end_index": v[-1]["j"],
                               **aggregate(records,v,bad)} for k,v in sorted(groups.items())]
    return {"status": "ok" if runs else "blocked", "reason": None if runs else "缺少连续且质检合格的时间、距离和海拔",
            "evidence_grade": "B" if runs else None, "total_ascent_m": ascent if runs else None,
            "total_descent_m": descent if runs else None, "natural_segments": segments, "distance_splits_m": splits,
            "split_method": "按区间起点实测距离分箱；跨边界区间完整归入起点箱，边界为原始采样点，不插值"}


def grade_groups(records, edges, bad):
    groups = defaultdict(list)
    for e in edges:
        if not e["usable"] or e["dz"] is None or not e["horizontal"] or e["horizontal"] < 0.5: continue
        g = e["dz"]/e["horizontal"]*100
        category = "flat" if abs(g)<3 else ("up_" if g>0 else "down_") + ("gentle" if abs(g)<8 else "moderate" if abs(g)<15 else "steep")
        groups[category].append(e)
    valid_times = [stamp(r.get("timestamp")) for r in records]
    midpoint = min(t for t in valid_times if t)+(max(t for t in valid_times if t)-min(t for t in valid_times if t))/2 if any(valid_times) else None
    def first(e):
        return midpoint is not None and valid_times[e["i"]] is not None and valid_times[e["i"]]<midpoint
    return [{"grade_bin": k, "all": aggregate(records,v,bad),
             "first_half": aggregate(records,[e for e in v if first(e)],bad),
             "second_half": aggregate(records,[e for e in v if not first(e)],bad)} for k,v in sorted(groups.items())]


def hr_dynamics(records, edges, bad, zones, zone_source, groups):
    if zones is not None:
        if zone_source not in ("user_max_hr", "user_lthr", "user_explicit") or len(zones)<2 or any(num(x) is None or x<=0 for x in zones) or any(a>=b for a,b in zip(zones,zones[1:])):
            raise ValueError("心率区间必须为递增正数边界，并提供 user_max_hr / user_lthr / user_explicit 来源。")
        seconds = [0.0]*(len(zones)-1); outside = 0.0
        for e in edges:
            hr = num(records[e["i"]].get("heart_rate"))
            if not e["usable"] or hr is None or e["i"] in bad["heart_rate"]: continue
            found = False
            for k,(lo,hi) in enumerate(zip(zones,zones[1:])):
                if lo <= hr < hi or (k==len(seconds)-1 and hr==hi):
                    seconds[k] += e["dt"]; found=True; break
            if not found: outside += e["dt"]
        zone_result = {"status": "ok", "boundaries_bpm": zones, "source": zone_source, "seconds": seconds, "outside_seconds": outside, "evidence_grade": "B"}
        if sum(seconds)+outside == 0:
            zone_result = blocked("没有质检合格且可计时的心率数据；不将缺失记作零秒")
    else: zone_result = blocked("未提供个人心率区间及来源，不使用年龄公式")
    # Same grade class alone does not establish same load, so require speed agreement and support.
    drift = []
    for group in groups:
        a,b = group["first_half"],group["second_half"]
        x,y = a["fields"]["heart_rate"]["mean"],b["fields"]["heart_rate"]["mean"]
        pa,pb = a["pace_seconds_per_km"]["value"],b["pace_seconds_per_km"]["value"]
        if not pa or not pb or not x["value"] or not y["value"] or min(x["support_seconds"],y["support_seconds"])<120: continue
        if abs(pa/pb-1)>0.1: continue
        drift.append({"grade_bin": group["grade_bin"], "hr_change_percent": (y["value"]/x["value"]-1)*100,
                      "evidence_grade": "C", "interpretation": "同坡度档、近似速度的心率变化候选；路面及环境未对齐，不能解释原因"})
    return {"zones": zone_result, "comparable_drift_candidates": drift,
            "recovery": blocked("恢复需要确认停留或下坡后连续且负荷可比的观察窗口，本版本不据单点生成恢复结论")}


def training_gate(pain_status, requested_stage):
    if pain_status not in (None,"unknown","unresolved","resolved"):
        raise ValueError("pain_status 仅接受 unknown / unresolved / resolved。")
    if requested_stage not in (None,"A","B","C","D","E"):
        raise ValueError("requested_stage 必须为 A–E。")
    locked = pain_status != "resolved"
    return {"status": "restricted" if locked else "requires_coach_review", "pain_status": pain_status or "unknown",
            "allowed_stage_candidates": ["A","B"] if locked else ["A","B","C","D","E"],
            "requested_stage": requested_stage, "requested_stage_allowed": requested_stage in ("A","B") if locked else None,
            "current_stage": None, "prohibited": ["intervals","hill_sprints","plyometrics","heavy_downhill"] if locked else [],
            "reason": "伤痛未消退或状态未知：仅开放 A/B 候选，禁止高强度" if locked else "单次成绩不能决定进阶，需教练结合持续恢复及训练背景确认",
            "source": "user_input", "diagnosis": None}


def fatigue_signals(groups):
    signals = []
    for group in groups:
        a,b = group["first_half"],group["second_half"]
        pa,pb = a["pace_seconds_per_km"],b["pace_seconds_per_km"]
        if not pa["value"] or not pb["value"] or min(pa["support_seconds"],pb["support_seconds"]) < 120:
            continue
        changes = {}
        if pb["value"]/pa["value"] >= 1.1:
            changes["pace_increase_percent"] = (pb["value"]/pa["value"]-1)*100
        for f in ("heart_rate","cadence","step_length"):
            x,y = a["fields"][f]["mean"],b["fields"][f]["mean"]
            if x["value"] and y["value"] and min(x["support_seconds"],y["support_seconds"])>=120 and abs(y["value"]/x["value"]-1)>=0.05:
                changes[f+"_change_percent"] = (y["value"]/x["value"]-1)*100
        if changes:
            signals.append({"grade_bin":group["grade_bin"],"changes":changes,"evidence_grade":"C",
                            "cause":None,"limitation":"同坡度档不等于同路面或同负荷，仅为变化候选"})
    return signals


def nutrition(entries, times):
    if entries is None: return blocked("无法评估摄入量：未提供补给记录")
    if not times or not times[0] or not times[-1] or times[-1]<=times[0]: return blocked("缺少有效活动起止时间")
    ordered = []
    for entry in entries:
        t = stamp(entry.get("timestamp"))
        if t is None or not times[0]<=t<=times[-1]: raise ValueError("补给时间必须处于活动起止时间内。")
        for key in ("carbohydrate_g","water_ml"):
            if entry.get(key) is not None and (num(entry[key]) is None or entry[key]<0): raise ValueError("补给量必须为非负有限数。")
        ordered.append((t,entry))
    ordered.sort(key=lambda x:x[0])
    hours = (times[-1]-times[0]).total_seconds()/3600
    out = {"status": "ok", "source": "user_log", "evidence_grade": "B", "duration_basis": "elapsed activity hours",
           "intervals_seconds": [(b[0]-a[0]).total_seconds() for a,b in zip(ordered,ordered[1:])]}
    for key in ("carbohydrate_g","water_ml"):
        # Empty explicit log represents no reported intake; incomplete log never becomes zero.
        complete = all(num(e.get(key)) is not None for _,e in ordered)
        out[key+"_per_hour"] = sum(e[key] for _,e in ordered)/hours if complete else None
    return out


def environment(records, edges, weather_loader=None, max_samples=6):
    bands = defaultdict(float)
    for e in edges:
        z = num(records[e["i"]].get("altitude"))
        if e["usable"] and z is not None and e["dz"] is not None:
            bands[int(z//500)*500] += e["dt"]
    eligible = [(i,r) for i,r in enumerate(records) if coordinate(r) and stamp(r.get("timestamp"))]
    out = {"altitude_bands_500m": [{"lower_m": k,"seconds":v} for k,v in sorted(bands.items())], "weather_samples": [],
           "weather_method": "原始轨迹点按活动 UTC 时间均匀选择，匹配最近小时；最大时间误差 3600 秒，不插值；天气为格点估计"}
    if weather_loader is None or not eligible:
        out["weather"] = blocked("未启用天气查询或缺少坐标时间，无法核实"); return out
    first,last = stamp(eligible[0][1]["timestamp"]),stamp(eligible[-1][1]["timestamp"])
    targets = [first+(last-first)*k/(max_samples-1) for k in range(max_samples)]
    chosen = sorted({min(eligible,key=lambda p:abs((stamp(p[1]["timestamp"])-t).total_seconds()))[0] for t in targets})
    for i in chosen:
        r = records[i]; lat,lon = coordinate(r)
        sample = {"record_index": i,"timestamp":r["timestamp"],"latitude":lat,"longitude":lon}
        try:
            sample["weather"] = weather_loader(lat,lon,stamp(r["timestamp"]))
            sample["status"] = "estimated"; sample["evidence_grade"] = "B"
        except (ValueError, OSError, TimeoutError) as exc:
            sample.update(status="unverified",reason=str(exc),weather=None,evidence_grade=None)
        out["weather_samples"].append(sample)
    count = sum(s["weather"] is not None for s in out["weather_samples"])
    out["weather"] = {"status": "estimated" if count==len(chosen) else "partial" if count else "unverified", "successful_samples": count}
    return out


def compare_activities(records, previous):
    if previous is None: return blocked("未提供第二条活动，不生成历史趋势")
    # A sparse spatial grid gives corridor candidates; ordering prevents reverse-route claims.
    cell = 0.001
    grid = defaultdict(list)
    for j,r in enumerate(previous):
        c = coordinate(r)
        if c: grid[(math.floor(c[0]/cell),math.floor(c[1]/cell))].append((j,c))
    matches = []; located = 0
    for i,r in enumerate(records):
        c = coordinate(r)
        if not c: continue
        located += 1
        x,y = math.floor(c[0]/cell),math.floor(c[1]/cell)
        lon_radius = min(180000,math.ceil(30/(111000*max(0.000001,math.cos(math.radians(c[0]))))/cell)+1)
        if lon_radius>100: return blocked("极区坐标超出当前轨迹对齐支持范围")
        candidates = [(haversine(c,p),j) for dx in (-1,0,1) for dy in range(-lon_radius,lon_radius+1) for j,p in grid.get((x+dx,y+dy),())]
        if candidates:
            error,j = min(candidates)
            if error <= 30: matches.append((i,j,error))
    overlap = len(matches)/located if located else None
    monotonic = all(a[1]<=b[1] for a,b in zip(matches,matches[1:]))
    return {"status": "alignment_only", "corridor_radius_m":30,"overlap_sample_fraction":overlap,
            "mean_alignment_error_m":sum(p[2] for p in matches)/len(matches) if matches else None,
            "max_alignment_error_m":max((p[2] for p in matches),default=None),
            "matched_sample_count":len(matches),"order_consistent":monotonic,
            "performance_conclusion_allowed":False,
            "reason":"重合不足或方向/顺序不一致" if overlap is None or overlap<0.8 or not monotonic else "走廊已对齐；尚需匹配地形、负荷和环境，能力进退结论仍被阻断",
            "evidence_grade":"B" if matches else None}


def analyze(records, summaries=None, hr_zone_boundaries=None, hr_zone_source=None,
            pain_status=None, requested_stage=None, nutrition_log=None, previous_records=None, weather_loader=None):
    summaries = summaries or {}
    gate = training_gate(pain_status,requested_stage)
    times,edges,bad,quality = prepare(records,summaries)
    terrain_result = terrain(records,edges,bad)
    groups = grade_groups(records,edges,bad)
    total = aggregate(records,edges,bad)
    elapsed = (times[-1]-times[0]).total_seconds() if times and times[0] and times[-1] and times[-1]>=times[0] else None
    device = next((num(s.get("total_ascent")) for s in summaries.get("session",[]) if num(s.get("total_ascent")) is not None),None)
    computed = terrain_result["total_ascent_m"]
    quality["ascent_comparison"] = {"device_m":device,"computed_m":computed,"difference_m":computed-device if computed is not None and device is not None else None}
    quality["cadence_stride_consistency"] = blocked("未确认 FIT 步频为步/分钟或周期/分钟及步幅单位，不自动套用速度关系")
    if elapsed is not None and any(a["kind"] in ("backwards_timestamp","invalid_timestamp") for a in quality["anomalies"]): elapsed = None
    return {"schema_version":"1.0", "status":"ok" if records else "insufficient_data",
        "methodology":{"policy":POLICY,"missing_values":"null = 未记录、无有效支撑或无法核实；不填零",
                       "averages":"左端采样值按相邻有效区间时长加权；不插值；缺口、暂停及异常字段排除",
                       "active_time":"FIT timer events excluding pauses; without events, observed time only, movement not confirmed",
                       "evidence":"A=质检通过的原始字段；B=口径明确派生值；C=候选信号；D=不生成数字",
                       "cadence":"保留 FIT 原生单位，未自动乘二；步幅单位保留源字段，不由速度反推",
                       "grade_groups":"相邻原始 GPS 水平距离至少 0.5m 的区间，以原始海拔差分档；可能受噪声影响。前后半程按活动 UTC 时间中点。分档 VAM 使用正海拔增量/上升区间时长，未施加迟滞，不与自然段 VAM 混用",
                       "series":"原始序列通过 get_fit_record_chunk 获取；图表须消费同一序列并标注任何平滑"},
        "overview": {"elapsed_seconds":elapsed,"active_time_status":"timer_event_adjusted_observed_time" if quality["pause_source"]!="unavailable" else "observed_time_only_movement_unconfirmed",
                     "timer_pause_seconds":sum(p["seconds"] for p in quality["pause_intervals"]) if quality["pause_source"]!="unavailable" else None,**total},
        "quality_report":quality,"terrain_segments":terrain_result,"grade_groups":groups,
        "hr_dynamics":hr_dynamics(records,edges,bad,hr_zone_boundaries,hr_zone_source,groups),
        "running_dynamics":{"availability":{f:quality["fields"][f] for f in DYNAMICS},"statistics":total["fields"],"by_terrain":groups},
        "fatigue_signals":fatigue_signals(groups),
        "environment":environment(records,edges,weather_loader),"nutrition":nutrition(nutrition_log,times),
        "compare_activities":compare_activities(records,previous_records),"training_gate":gate,
        "blocked_conclusions":["疲劳原因与天气补给因果关系","伤痛诊断","无影像的技术路况评分","仅凭一次活动决定训练进阶"]}
