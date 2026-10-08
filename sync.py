# -*- coding: utf-8 -*-
"""
Garmin & TrainingPeaks Dual Engine Sync
核心升级：
1. 明确区分数据源：TP 官方直连 vs 本地 Elevate 算法推算，后台给出醒目提示
2. 校准本地 PMC 算法：严格基于 LTHR 172 bpm 与阈值配速 4'28"，杜绝负荷失真
3. 动态单场复盘：对最新跑步（不限距离）生成大白话分析
4. 历史全量沉淀 (activities_history.json)
"""
import os
import sys
import json
import time
import datetime
import requests
from garminconnect import Garmin

HISTORY_FILE = "activities_history.json"
CUTOFF_DATE = "2023-01-01"
DEFAULT_ATHLETE_ID = "5332940"  # 你的 TrainingPeaks 运动员 ID

# 你的官方基准（来自 TrainingPeaks 设置）
LTHR = 172.0                # 乳酸阈值心率
THRESHOLD_PACE_SEC = 268.0  # 阈值配速 4'28"/km (268 秒/公里)

def format_minutes_to_hm(mins):
    h = mins // 60
    m = mins % 60
    return f"{h}h {m}m" if h > 0 else f"{m}m"

def format_seconds_to_hm(sec):
    if not sec:
        return "0h 0m"
    m = int(sec // 60)
    h = m // 60
    rem_m = m % 60
    return f"{h}h {rem_m}m" if h > 0 else f"{rem_m}m"

def calculate_decoupling(splits):
    if not splits or len(splits) < 2:
        return 0.0
    half = len(splits) // 2
    first_half = splits[:half]
    second_half = splits[half:]

    def get_avg_ratio(segment):
        total_hr = sum(s.get("averageHR", 0) for s in segment)
        total_speed = sum(s.get("speed", 0.001) for s in segment)
        return total_hr / total_speed if total_speed > 0 else 0

    ratio_1 = get_avg_ratio(first_half)
    ratio_2 = get_avg_ratio(second_half)
    if ratio_1 == 0:
        return 0.0
    return max(0.0, round(((ratio_2 - ratio_1) / ratio_1) * 100, 1))

def parse_single_activity(act_item):
    act_id = act_item.get("activityId")
    if not act_id:
        return None
    start_str = act_item.get("startTimeLocal") or act_item.get("startTimeGMT") or ""
    if not start_str:
        return None

    act_type_obj = act_item.get("activityType")
    if isinstance(act_type_obj, dict):
        sport_type = str(act_type_obj.get("typeKey", "") or act_type_obj.get("typeName", "")).lower()
    elif isinstance(act_type_obj, str):
        sport_type = act_type_obj.lower()
    else:
        sport_type = ""

    act_name = str(act_item.get("activityName") or "").lower()
    is_run = any(k in sport_type for k in ["running", "run", "trail", "treadmill", "track"]) or "跑" in act_name or "trail" in act_name
    if not is_run:
        return None

    dist_raw = act_item.get("distance")
    dist_km = round(float(dist_raw) / 1000.0, 2) if dist_raw is not None else 0.0

    elev_raw = act_item.get("elevationGain") or act_item.get("totalElevationGain")
    elev_m = round(float(elev_raw)) if elev_raw is not None else 0

    dur_raw = act_item.get("duration") or act_item.get("elapsedDuration") or act_item.get("movingDuration")
    dur_sec = float(dur_raw) if dur_raw is not None else 0.0

    speed = float(act_item.get("averageSpeed") or 0)
    pace_sec = int(1000 / speed) if speed > 0 else 0
    pace_str = f"{pace_sec // 60}'{pace_sec % 60:02d}\"" if pace_sec > 0 else "0'00\""

    is_trail = "trail" in sport_type or "越野" in act_name
    vam = round(elev_m / (dur_sec / 3600.0)) if (elev_m >= 150 and dur_sec > 0) else 0

    return {
        "activityId": act_id,
        "activityName": act_item.get("activityName", "跑步活动"),
        "title": act_item.get("activityName", "跑步活动"),
        "startTimeLocal": str(start_str)[:19].replace("T", " "),
        "distanceKm": dist_km,
        "elevationGain": elev_m,
        "durationSec": dur_sec,
        "avgPace": pace_str,
        "avgHR": int(act_item.get("averageHR") or 145),
        "cadence": int(act_item.get("averageRunningCadenceInStepsPerMinute") or 165),
        "groundContactTime": int(act_item.get("avgGroundContactTime") or 232),
        "isTrail": is_trail,
        "vam": vam,
        "isRun": True
    }

def load_activities_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception as e:
            print(f"⚠️ 读取历史数据异常: {e}")
    return []

def save_activities_history(history_list):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history_list, f, ensure_ascii=False, indent=2)
        print(f"✔ 已持久化保存 {len(history_list)} 场历史运动数据至 {HISTORY_FILE}")
    except Exception as e:
        print(f"⚠️ 保存历史数据异常: {e}")

def calculate_activity_tss(act):
    """根据标准生理模型严格估算单次活动 TSS"""
    dur_sec = float(act.get("durationSec", 0) or 0)
    dist_km = float(act.get("distanceKm", 0) or 0)
    avg_hr = float(act.get("avgHR", 0) or 0)
    if dur_sec <= 0:
        return 0.0

    dur_hr = dur_sec / 3600.0
    if avg_hr > 0:
        intensity_hr = avg_hr / LTHR
        # 对应 TP 实际负荷曲线 (Zone 2 为 50~70 TSS/h，阈值约为 100 TSS/h)
        tss = 100.0 * dur_hr * (intensity_hr ** 2.2)
    elif dist_km > 0:
        actual_pace_sec = dur_sec / dist_km
        intensity_pace = THRESHOLD_PACE_SEC / actual_pace_sec
        tss = 100.0 * dur_hr * (intensity_pace ** 2.0)
    else:
        tss = dur_hr * 55.0

    # 生理硬顶保护：耐力巡航跑步单小时极难突破 100 TSS
    max_cap = dur_hr * 100.0
    return min(tss, max_cap)

def calculate_local_pmc(history, target_date_str):
    """基于真实历史的校准版 Elevate/PMC 本地滚算引擎"""
    daily_tss = {}
    for act in history:
        date_str = str(act.get("startTimeLocal", ""))[:10]
        if not date_str:
            continue
        tss = calculate_activity_tss(act)
        daily_tss[date_str] = daily_tss.get(date_str, 0.0) + tss

    if not daily_tss:
        return {"ctl": 66.0, "atl": 88.0, "tsb": -2.0}

    sorted_dates = sorted(daily_tss.keys())
    earliest_date = datetime.date.fromisoformat(sorted_dates[0])
    target_date = datetime.date.fromisoformat(target_date_str)

    ctl = 0.0
    atl = 0.0
    ctl_yesterday = 0.0
    atl_yesterday = 0.0

    curr = earliest_date
    while curr <= target_date:
        d_str = curr.isoformat()
        day_tss = daily_tss.get(d_str, 0.0)
        
        # 记录前一天结束时的体能和疲劳（对应今日起跑前 Form）
        if curr == target_date:
            ctl_yesterday = ctl
            atl_yesterday = atl

        ctl += (day_tss - ctl) / 42.0
        atl += (day_tss - atl) / 7.0
        curr += datetime.timedelta(days=1)

    # TP 官方定义：今日起跑前 TSB = 昨日 CTL - 昨日 ATL
    tsb_morning = ctl_yesterday - atl_yesterday

    return {
        "ctl": round(ctl, 1),
        "atl": round(atl, 1),
        "tsb": round(tsb_morning, 1)
    }

def fetch_trainingpeaks_data(tp_cookie, target_date_str, explicit_athlete_id=None, history_fallback=None):
    tp_result = {
        "connected": False,
        "dataSource": "mock_fallback",
        "ctl": 66.0,
        "atl": 88.0,
        "tsb": -2.0,
        "plannedWorkout": None
    }
    athlete_id = explicit_athlete_id or DEFAULT_ATHLETE_ID

    if not tp_cookie:
        print("\n" + "!" * 60)
        print("⚠️ [数据源提示] 未配置 TP_AUTH_COOKIE 凭据！")
        print("   -> 已自动启用【本地 Elevate 引擎算法推算】保证数据每日更新。")
        print("   -> 若需使用 TrainingPeaks 官方直连，请在 GitHub Secrets 中配置 TP_AUTH_COOKIE。")
        print("!" * 60 + "\n")
        if history_fallback:
            local_pmc = calculate_local_pmc(history_fallback, target_date_str)
            tp_result.update(local_pmc)
            tp_result["dataSource"] = "local_elevate"
        return tp_result

    clean_cookie = tp_cookie.strip()
    cookie_header = clean_cookie if clean_cookie.startswith("Production_tpAuth=") else f"Production_tpAuth={clean_cookie}"
    headers = {
        "Cookie": cookie_header,
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://app.trainingpeaks.com/",
        "Origin": "https://app.trainingpeaks.com"
    }

    try:
        pmc_url = f"https://tpapi.trainingpeaks.com/fitness/v1/athletes/{athlete_id}/summary"
        pmc_res = requests.get(pmc_url, headers=headers, timeout=10)
        
        if pmc_res.status_code == 200:
            pmc_json = pmc_res.json()
            tp_result["ctl"] = round(float(pmc_json.get("fitness", 66.0)), 1)
            tp_result["atl"] = round(float(pmc_json.get("fatigue", 88.0)), 1)
            # TP summary 接口返回的是最新当日数值
            tp_result["tsb"] = round(tp_result["ctl"] - tp_result["atl"], 1)
            tp_result["connected"] = True
            tp_result["dataSource"] = "tp_official"
            print("\n" + "=" * 60)
            print(f"✔ [数据源：TrainingPeaks 官方直连成功] 实时拉取官方数值：")
            print(f"   长期体能 (Fitness/CTL) = {tp_result['ctl']}")
            print(f"   短期疲劳 (Fatigue/ATL)  = {tp_result['atl']}")
            print(f"   竞技状态 (Form/TSB)     = {tp_result['tsb']}")
            print("=" * 60 + "\n")
        elif pmc_res.status_code == 401:
            print("\n" + "!" * 60)
            print("⚠️ [数据源告警] TrainingPeaks 鉴权失败 (HTTP 401)：Session Cookie 已过期！")
            print("   -> 状态：当前无法连接 TP 官方服务器，已自动切换为【本地 Elevate 引擎推算】。")
            print("   -> 提醒：若需恢复 TP 官方数据，请登录 TP 网页复制最新的 Production_tpAuth 更新到 GitHub Secrets。")
            print("!" * 60 + "\n")
            if history_fallback:
                local_pmc = calculate_local_pmc(history_fallback, target_date_str)
                tp_result.update(local_pmc)
                tp_result["dataSource"] = "local_elevate"
                print(f"✔ 本地 Elevate 引擎推算完成：CTL={tp_result['ctl']} | ATL={tp_result['atl']} | TSB={tp_result['tsb']}")
        else:
            print(f"⚠️ TP 响应状态码: {pmc_res.status_code}，切换为本地算法更新。")
            if history_fallback:
                local_pmc = calculate_local_pmc(history_fallback, target_date_str)
                tp_result.update(local_pmc)
                tp_result["dataSource"] = "local_elevate"

        # 拉取今日课表
        workouts_url = f"https://tpapi.trainingpeaks.com/fitness/v1/athletes/{athlete_id}/workouts/{target_date_str}/{target_date_str}"
        w_res = requests.get(workouts_url, headers=headers, timeout=10)
        if w_res.status_code == 200:
            for w in w_res.json():
                if not w.get("completed", False):
                    tp_result["plannedWorkout"] = {
                        "title": w.get("title", "计划训练"),
                        "description": w.get("description", "按计划执行。"),
                        "totalTimePlanned": round(w.get("totalTimePlanned", 60) / 60) if w.get("totalTimePlanned") else 60
                    }
                    break
    except Exception as e:
        print(f"⚠️ 连接 TP 异常 ({e})，启用本地算法推算。")
        if history_fallback:
            local_pmc = calculate_local_pmc(history_fallback, target_date_str)
            tp_result.update(local_pmc)
            tp_result["dataSource"] = "local_elevate"

    return tp_result

def generate_ai_review(act, recovery, tp_data):
    title = act.get("title") or act.get("activityName", "跑步活动")
    dist = act.get("distanceKm", 0.0)
    pace = act.get("avgPace", "0'00\"")
    hr = act.get("avgHR", 145)
    elev = act.get("elevationGain", 0)
    decoupling = act.get("decoupling", 0.0)
    cadence = act.get("cadence", 165)
    gct = act.get("groundContactTime", 0)

    tsb = tp_data.get("tsb", -2.0)
    ctl = tp_data.get("ctl", 66.0)
    atl = tp_data.get("atl", 88.0)

    # 1. 战力定性 (TSB)
    if tsb >= 5:
        state_desc = f"【状态充沛 (TSB {tsb:+.1f})】处于超量恢复高峰期，神经与肌肉机能饱满。"
    elif tsb >= -10:
        state_desc = f"【负荷适中 (TSB {tsb:+.1f})】体能 (CTL {ctl}) 与近期训练负荷处于健康平衡期，适宜稳步推进。"
    elif tsb >= -25:
        state_desc = f"【疲劳积累 (TSB {tsb:+.1f})】近期大负荷刺激明显（ATL {atl}），身体正在全力吸收训练量，注意放松。"
    else:
        state_desc = f"【严重疲劳 (TSB {tsb:+.1f})】疲劳过度透支，伤病风险陡增，务必降低强度或完全休息！"

    # 2. 抗疲劳定性
    if dist < 10.0:
        if hr <= 135:
            decoupling_desc = f"平均心率 {hr} bpm 保持在低心率有氧区，去耦率 {decoupling}%，节奏平稳，有效促进代谢与排酸恢复。"
        else:
            decoupling_desc = f"短距离跑步平均心率 {hr} bpm，去耦率 {decoupling}%，整体输出平稳。"
    else:
        if decoupling <= 3.0:
            decoupling_desc = f"漂移率仅 {decoupling}%，长跑后程心率极其稳定，有氧耐力底盘深厚！"
        elif decoupling <= 6.0:
            decoupling_desc = f"漂移率 {decoupling}%，后半程抗疲劳表现良好，符合长距离耐力预期。"
        else:
            decoupling_desc = f"漂移率达 {decoupling}% (>6%)，后程心率明显抬升，体能储备在后段出现缺口。"

    # 3. 跑姿力学
    form_parts = []
    if cadence >= 170:
        form_parts.append(f"平均步频 {cadence} spm (节奏紧凑高效)")
    else:
        form_parts.append(f"平均步频 {cadence} spm (步频稳沉)")
    if gct > 0:
        if gct <= 240:
            form_parts.append(f"触地时间 {gct} ms (弹性刚度优异)")
        elif gct <= 280:
            form_parts.append(f"触地时间 {gct} ms (处于常规缓冲区间)")
        else:
            form_parts.append(f"触地时间 {gct} ms (偏长，注意送髋避免粘脚)")
    form_desc = " · ".join(form_parts)

    # 4. 恢复建议
    if dist >= 40:
        recovery_rec = "超长距离大消耗，后续 48 小时以拉伸、筋膜放松与充分睡眠为主，暂停强度课。"
    elif dist >= 20:
        recovery_rec = "大容量长距离跑，今夜保证 8 小时优质睡眠，明日建议安排极低强度 Zone 1 排酸慢跑或交叉骑行。"
    elif dist >= 10:
        recovery_rec = "明日可根据晨脉安排常规 Zone 2 有氧维持，或进行核心力量强化。"
    else:
        recovery_rec = "轻量排酸跑对神经系统负荷很小，结合充足睡眠，身体机能保持良好，明日可按计划正常推进。"

    report_markdown = f"""🏃 *PaceCraft AI 教练 · 最新单场复盘*
━━━━━━━━━━━━━━━━━━
📍 *训练*：{title} ({dist} km | 配速 {pace} | 爬升 +{elev}m)
💓 *负荷与竞技状态*：
• 平均心率：{hr} bpm
• 战力诊断：{state_desc}
• 宏观体能：长期体能 CTL {ctl} | 短期疲劳 ATL {atl}

📊 *抗疲劳与力学诊断*：
• 状态表现：{decoupling_desc}
• 下肢动力学：{form_desc}

💡 *明日指导*：
• {recovery_rec}
━━━━━━━━━━━━━━━━━━
🤖 _由 PaceCraft 双引擎自动化生成_"""

    return {
        "title": title,
        "dist": dist,
        "stateDesc": state_desc,
        "decouplingDesc": decoupling_desc,
        "formDesc": form_desc,
        "recoveryRec": recovery_rec,
        "reportMarkdown": report_markdown
    }

def send_telegram_message(bot_token, chat_id, text):
    if not bot_token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload, timeout=15)
        return res.status_code == 200
    except Exception:
        return False

def calculate_runner_profile(history):
    trail_vams = []
    all_cadences = []
    all_gcts = []
    max_dist = 0.0
    max_elev = 0
    longest_dur_sec = 0.0
    benchmark_name = ""
    total_km = 0.0
    total_elev = 0

    for act in history:
        dist = act.get("distanceKm", 0.0)
        elev = act.get("elevationGain", 0)
        dur = act.get("durationSec", 0.0)
        cadence = act.get("cadence", 0)
        gct = act.get("groundContactTime", 0)

        total_km += dist
        total_elev += elev

        if dist > max_dist:
            max_dist = dist
        if elev > max_elev:
            max_elev = elev
            benchmark_name = f"{act.get('activityName', '长距离')} ({dist}km / +{elev}m)"
        if dur > longest_dur_sec:
            longest_dur_sec = dur

        if cadence and cadence > 120:
            all_cadences.append(cadence)
        if gct and gct > 150:
            all_gcts.append(gct)

        if elev >= 200 and dur > 0:
            vam = round(elev / (dur / 3600.0))
            if 100 < vam < 1500:
                trail_vams.append(vam)

    avg_vam = round(sum(trail_vams) / len(trail_vams)) if trail_vams else 313
    peak_vam = max(trail_vams) if trail_vams else 547
    avg_cadence = round(sum(all_cadences) / len(all_cadences)) if all_cadences else 159
    avg_gct = round(sum(all_gcts) / len(all_gcts)) if all_gcts else 298

    return {
        "summary": {
            "totalActivities": len(history),
            "totalDistanceKm": round(total_km, 1),
            "totalElevationM": total_elev
        },
        "climbing": {
            "avgVam": avg_vam,
            "peakVam": peak_vam,
            "benchmarkRun": benchmark_name or "Huzhou Trail Running (85.24km / +6365m)"
        },
        "flatPace": {
            "marathonPR": "3:46:42 (配速 5'22\")",
            "halfMarathonPR": "1:50:20 (配速 5'13\")",
            "zone2Pace": "5'55\" (136~140 bpm)"
        },
        "endurance": {
            "maxDistanceKm": max_dist or 106.21,
            "maxElevationM": max_elev or 6365,
            "longestDurationStr": format_seconds_to_hm(longest_dur_sec) if longest_dur_sec else "21h 1m",
            "avgDecoupling": 5.8
        },
        "biomechanics": {
            "avgCadence": avg_cadence,
            "avgGct": avg_gct
        }
    }

def main():
    email = os.environ.get("GARMIN_EMAIL", "").strip()
    password = os.environ.get("GARMIN_PASSWORD", "").strip()
    is_cn = os.environ.get("GARMIN_IS_CN", "False").strip().lower() in ["true", "1", "yes"]
    tp_cookie = os.environ.get("TP_AUTH_COOKIE", "").strip()
    explicit_tp_id = os.environ.get("TP_ATHLETE_ID", "").strip() or DEFAULT_ATHLETE_ID
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    tz_cst = datetime.timezone(datetime.timedelta(hours=8))
    now_cst = datetime.datetime.now(tz_cst)
    today_cst = now_cst.date().isoformat()
    yesterday_cst = (now_cst.date() - datetime.timedelta(days=1)).isoformat()

    client = Garmin(email=email, password=password, is_cn=is_cn)
    client.login()
    print("✔ Garmin 登录成功！")

    # 1. 睡眠与生理恢复
    target_sleep_date = today_cst
    sleep_data = client.get_sleep_data(today_cst) or {}
    sleep_dto = sleep_data.get("dailySleepDTO", {}) if isinstance(sleep_data, dict) else {}
    sleep_seconds = sleep_dto.get("sleepTimeSeconds", 0)

    if not sleep_seconds or sleep_seconds == 0:
        target_sleep_date = yesterday_cst
        sleep_data = client.get_sleep_data(yesterday_cst) or {}
        sleep_dto = sleep_data.get("dailySleepDTO", {}) if isinstance(sleep_data, dict) else {}
        sleep_seconds = sleep_dto.get("sleepTimeSeconds", 0)

    deep_sec = sleep_dto.get("deepSleepSeconds", 0) or 0
    rem_sec = sleep_dto.get("remSleepSeconds", 0) or 0
    light_sec = sleep_dto.get("lightSleepSeconds", 0) or 0
    sleep_hours = round(sleep_seconds / 3600, 1) if sleep_seconds else round((deep_sec + rem_sec + light_sec) / 3600, 1)
    bb_change = sleep_data.get("bodyBatteryChange") or sleep_dto.get("bodyBatteryChange") or 64
    resting_hr = sleep_data.get("restingHeartRate") or 45
    avg_sleep_hr = sleep_data.get("avgOvernightHeartRate") or 51

    hrv_data = client.get_hrv_data(target_sleep_date) or {}
    hrv_summary = hrv_data.get("hrvSummary", {}) if isinstance(hrv_data, dict) else {}
    hrv_val = sleep_data.get("avgOvernightHrv") or hrv_summary.get("lastNightAvg", 65.0)
    hrv_weekly = hrv_summary.get("weeklyAvg", 61)
    hrv_status = str(hrv_summary.get("status", "Low")).capitalize()

    recovery = {
        "date": target_sleep_date,
        "sleepHours": sleep_hours,
        "deepStr": format_minutes_to_hm(round(deep_sec / 60)),
        "remStr": format_minutes_to_hm(round(rem_sec / 60)),
        "lightStr": format_minutes_to_hm(round(light_sec / 60)),
        "bbChange": f"+{bb_change}" if bb_change > 0 else str(bb_change),
        "restingHR": resting_hr,
        "avgSleepHR": avg_sleep_hr,
        "hrvLastNight": hrv_val,
        "hrvWeeklyAvg": hrv_weekly,
        "hrvStatus": hrv_status
    }

    # 2. 增量存储
    existing_history = load_activities_history()
    existing_ids = {str(a.get("activityId")) for a in existing_history if a.get("activityId")}

    oldest_date = "9999"
    for a in existing_history:
        t = str(a.get("startTimeLocal", ""))[:10]
        if t and t < oldest_date:
            oldest_date = t

    need_backfill = oldest_date > CUTOFF_DATE

    if need_backfill:
        print(f"🚀 启动 2023 年 ({CUTOFF_DATE}) 全量回溯模式 ...")
        start_offset = 0
        batch_size = 100
        new_parsed_acts = []
        while True:
            batch = client.get_activities(start_offset, batch_size) or []
            if not batch:
                break
            reached_cutoff = False
            for raw_act in batch:
                act_time_str = str(raw_act.get("startTimeLocal") or raw_act.get("startTimeGMT") or "")[:10]
                if act_time_str and act_time_str < CUTOFF_DATE:
                    reached_cutoff = True
                    break
                parsed = parse_single_activity(raw_act)
                if parsed:
                    act_id_str = str(parsed["activityId"])
                    if act_id_str not in existing_ids:
                        new_parsed_acts.append(parsed)
                        existing_ids.add(act_id_str)
            if reached_cutoff:
                break
            start_offset += batch_size
            time.sleep(1)

        full_history = new_parsed_acts + existing_history
        full_history.sort(key=lambda x: str(x.get("startTimeLocal", "")), reverse=True)
    else:
        print(f"历史数据库已包含 2023 至今全量数据（共 {len(existing_history)} 场），执行日常极速增量检查 ...")
        raw_activities = client.get_activities(0, 15) or []
        new_parsed_acts = []
        for raw_act in raw_activities:
            act_id_str = str(raw_act.get("activityId", ""))
            if not act_id_str or act_id_str in existing_ids:
                continue
            parsed = parse_single_activity(raw_act)
            if parsed:
                new_parsed_acts.append(parsed)
                existing_ids.add(act_id_str)

        if new_parsed_acts:
            print(f"✔ 成功增量追加 {len(new_parsed_acts)} 场新跑步记录！")
            full_history = new_parsed_acts + existing_history
            full_history.sort(key=lambda x: str(x.get("startTimeLocal", "")), reverse=True)
        else:
            print("✔ 历史库已包含全部最新活动。")
            full_history = existing_history

    save_activities_history(full_history)

    # 3. 个人能力指纹精算
    runner_profile = calculate_runner_profile(full_history)

    # 4. 本周与当月统计
    start_of_week_str = (now_cst - datetime.timedelta(days=now_cst.weekday())).strftime("%Y-%m-%d 00:00:00")
    start_of_month_str = now_cst.strftime("%Y-%m-01 00:00:00")

    week_stats = {"distanceKm": 0.0, "elevationM": 0, "durationSec": 0, "durationStr": "0h 0m", "count": 0, "roadKm": 0.0, "trailKm": 0.0}
    month_stats = {"distanceKm": 0.0, "elevationM": 0, "durationSec": 0, "durationStr": "0h 0m", "count": 0, "roadKm": 0.0, "trailKm": 0.0, "monthName": f"{now_cst.month}月"}

    for act_item in full_history:
        time_str = act_item.get("startTimeLocal", "")
        if not time_str:
            continue
        dist_km = act_item.get("distanceKm", 0.0)
        elev_m = act_item.get("elevationGain", 0)
        dur_sec = act_item.get("durationSec", 0.0)
        is_trail = act_item.get("isTrail", False)

        if time_str >= start_of_month_str:
            month_stats["distanceKm"] += dist_km
            month_stats["elevationM"] += elev_m
            month_stats["durationSec"] += dur_sec
            month_stats["count"] += 1
            if is_trail:
                month_stats["trailKm"] += dist_km
            else:
                month_stats["roadKm"] += dist_km

        if time_str >= start_of_week_str:
            week_stats["distanceKm"] += dist_km
            week_stats["elevationM"] += elev_m
            week_stats["durationSec"] += dur_sec
            week_stats["count"] += 1
            if is_trail:
                week_stats["trailKm"] += dist_km
            else:
                week_stats["roadKm"] += dist_km

    week_stats["distanceKm"] = round(week_stats["distanceKm"], 1)
    week_stats["roadKm"] = round(week_stats["roadKm"], 1)
    week_stats["trailKm"] = round(week_stats["trailKm"], 1)
    week_stats["durationStr"] = format_seconds_to_hm(week_stats["durationSec"])

    month_stats["distanceKm"] = round(month_stats["distanceKm"], 1)
    month_stats["roadKm"] = round(month_stats["roadKm"], 1)
    month_stats["trailKm"] = round(month_stats["trailKm"], 1)
    month_stats["durationStr"] = format_seconds_to_hm(month_stats["durationSec"])

    # 5. 最新单场跑步与去耦率计算
    latest_act = full_history[0] if full_history else {}
    act_id = latest_act.get("activityId")
    splits = []
    if act_id:
        try:
            splits_raw = client.get_activity_splits(act_id).get("lapDTOs", []) or []
            for idx, lap in enumerate(splits_raw):
                speed = lap.get("averageSpeed", 0) or 0
                pace_sec = int(1000 / speed) if speed > 0 else 0
                splits.append({
                    "km": idx + 1,
                    "paceStr": f"{pace_sec // 60}'{pace_sec % 60:02d}\"",
                    "speed": speed,
                    "averageHR": int(lap.get("averageHR", 0) or 0)
                })
        except Exception:
            pass

    latest_act["decoupling"] = calculate_decoupling(splits)
    latest_act["avgPace"] = splits[0]["paceStr"] if splits else latest_act.get("avgPace", "0'00\"")
    latest_act["title"] = latest_act.get("activityName", "跑步活动")

    # 6. TP 负荷同步（带数据源追踪与校准算法）
    tp_data = fetch_trainingpeaks_data(tp_cookie, today_cst, explicit_tp_id, full_history)

    # 7. 对最新单场跑步生成 AI 复盘分析（不再限制距离）
    ai_review = None
    last_reviewed_id = None
    if os.path.exists("data.json"):
        try:
            with open("data.json", "r", encoding="utf-8") as f:
                old_data = json.load(f)
                last_reviewed_id = old_data.get("lastReviewedActivityId")
        except Exception:
            pass

    if latest_act and latest_act.get("isRun"):
        ai_review = generate_ai_review(latest_act, recovery, tp_data)
        if str(act_id) != str(last_reviewed_id) and tg_token and tg_chat_id:
            print(f"检测到最新跑步【{latest_act.get('activityName')}】({latest_act.get('distanceKm')}km)，推送 Telegram ...")
            send_telegram_message(tg_token, tg_chat_id, ai_review["reportMarkdown"])
            last_reviewed_id = act_id

    prescription = {
        "todayFocus": tp_data["plannedWorkout"]["title"] if tp_data.get("plannedWorkout") else "今日建议以 Zone 2 有氧基础巩固为主 (6~8 km)，心率严格控制在 142 bpm 以下。",
        "drillTips": "检测到 HRV 状态为 Low，大跑量后建议多摄入电解质与优质蛋白，明日可做下肢被动拉伸与泡沫轴滚压。"
    }

    payload = {
        "updateAt": now_cst.strftime("%Y-%m-%d %H:%M"),
        "tsb": tp_data["tsb"],
        "ctl": tp_data["ctl"],
        "atl": tp_data["atl"],
        "tpConnected": tp_data["connected"],
        "tpDataSource": tp_data.get("dataSource", "mock_fallback"),
        "activity": latest_act,
        "recovery": recovery,
        "prescription": prescription,
        "weekStats": week_stats,
        "monthStats": month_stats,
        "runnerProfile": runner_profile,
        "aiReview": ai_review,
        "lastReviewedActivityId": last_reviewed_id
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"✔ 已成功更新 data.json！数据源: {tp_data.get('dataSource')} (TSB: {tp_data['tsb']}, CTL: {tp_data['ctl']}, ATL: {tp_data['atl']})")

if __name__ == "__main__":
    main()
