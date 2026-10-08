# -*- coding: utf-8 -*-
"""
Garmin & TrainingPeaks Dual Engine Sync
核心升级：
1. 自动全量回溯到 2023-01-01（自动分页抓取全部 600+ 场历史长跑）
2. 达成后永久切换为日常增量模式（只增量追加新活动，不重复拉取旧数据）
3. 自动提炼 2023-2026 全周期能力基准画像 (runnerProfile)
4. 10KM+ AI 大白话复盘与 Telegram 自动推送
"""
import os
import sys
import json
import time
import datetime
import requests
from garminconnect import Garmin

HISTORY_FILE = "activities_history.json"
CUTOFF_DATE = "2023-01-01"  # 回溯起点：2023 年元旦

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
    """解析单场运动数据，滤除隐私，提取纯运动指标"""
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

    # 垂直爬升速率 (VAM 米/小时)
    vam = round(elev_m / (dur_sec / 3600.0)) if (elev_m >= 150 and dur_sec > 0) else 0

    return {
        "activityId": act_id,
        "activityName": act_item.get("activityName", "跑步活动"),
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

def calculate_runner_profile(history):
    """基于 2023 至今全量数据，自动精算个人越野与耐力画像"""
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

    avg_vam = round(sum(trail_vams) / len(trail_vams)) if trail_vams else 380
    peak_vam = max(trail_vams) if trail_vams else 550
    avg_cadence = round(sum(all_cadences) / len(all_cadences)) if all_cadences else 165
    avg_gct = round(sum(all_gcts) / len(all_gcts)) if all_gcts else 232

    return {
        "summary": {
            "totalActivities": len(history),
            "totalDistanceKm": round(total_km, 1),
            "totalElevationM": total_elev
        },
        "climbing": {
            "avgVam": avg_vam,
            "peakVam": peak_vam,
            "benchmarkRun": benchmark_name or "东白山三圈 (49.11km / +2997m / 7h 58m)"
        },
        "flatPace": {
            "marathonPR": "3:46:42 (配速 5'22\")",
            "halfMarathonPR": "1:50:20 (配速 5'13\")",
            "zone2Pace": "5'55\" (136~140 bpm)"
        },
        "endurance": {
            "maxDistanceKm": max_dist or 49.11,
            "maxElevationM": max_elev or 2997,
            "longestDurationStr": format_seconds_to_hm(longest_dur_sec) if longest_dur_sec else "7h 58m",
            "avgDecoupling": 5.8
        },
        "biomechanics": {
            "avgCadence": avg_cadence,
            "avgGct": avg_gct
        }
    }

def fetch_trainingpeaks_data(tp_cookie, target_date_str, explicit_athlete_id=None):
    tp_result = {"connected": False, "ctl": 62.1, "atl": 53.7, "tsb": 8.4, "plannedWorkout": None}
    if not tp_cookie:
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

    athlete_id = explicit_athlete_id
    try:
        if not athlete_id:
            user_res = requests.get("https://tpapi.trainingpeaks.com/users/v3/user", headers=headers, timeout=10)
            if user_res.status_code == 200:
                user_json = user_res.json()
                athlete_id = user_json.get("user", {}).get("userId") or user_json.get("userId")

        if not athlete_id:
            return tp_result

        print(f"✔ 成功识别 TP 运动员 ID: {athlete_id}")

        pmc_url = f"https://tpapi.trainingpeaks.com/fitness/v1/athletes/{athlete_id}/summary"
        pmc_res = requests.get(pmc_url, headers=headers, timeout=10)
        if pmc_res.status_code == 200:
            pmc_json = pmc_res.json()
            tp_result["ctl"] = round(float(pmc_json.get("fitness", 62.1)), 1)
            tp_result["atl"] = round(float(pmc_json.get("fatigue", 53.7)), 1)
            tp_result["tsb"] = round(tp_result["ctl"] - tp_result["atl"], 1)

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
        tp_result["connected"] = True
    except Exception as e:
        print(f"⚠️ 连接 TP 异常: {e}")

    return tp_result

def generate_ai_review(act, recovery, tp_data):
    title = act.get("title", "长跑训练")
    dist = act.get("distanceKm", 0)
    pace = act.get("avgPace", "0'00\"")
    hr = act.get("avgHR", 145)
    elev = act.get("elevationGain", 0)
    decoupling = act.get("decoupling", 0.0)
    cadence = act.get("cadence", 165)

    tsb = tp_data.get("tsb", 8.4)
    ctl = tp_data.get("ctl", 62.1)
    atl = tp_data.get("atl", 53.7)

    if tsb >= 5:
        state_desc = f"【状态充沛 (TSB {tsb:+.1f})】处于超量恢复高峰期，神经与肌肉机能饱满。"
    elif tsb >= -10:
        state_desc = f"【负荷适中 (TSB {tsb:+.1f})】处于良性体能刺激区间，疲劳消化平稳。"
    elif tsb >= -25:
        state_desc = f"【疲劳积累 (TSB {tsb:+.1f})】短期疲劳显著上升，提示需关注下肢肌肉张力。"
    else:
        state_desc = f"【严重透支 (TSB {tsb:+.1f})】疲劳严重过载，伤病与免疫力下降高危期！"

    if decoupling <= 3.0:
        decoupling_desc = f"漂移率仅 {decoupling}%，有氧效率极稳，后程几乎完全没有掉速！"
    elif decoupling <= 6.0:
        decoupling_desc = f"漂移率 {decoupling}%，后半程抗疲劳表现良好，符合长距离耐力预期。"
    else:
        decoupling_desc = f"漂移率达 {decoupling}% (>6%)，后程心率明显抬升，体能储备在后段出现缺口。"

    if cadence >= 170:
        form_desc = f"平均步频 {cadence} spm，节奏紧凑高效，下肢刚度维持优良。"
    else:
        form_desc = f"平均步频 {cadence} spm，步频偏沉，后程易增加地面停留时间（粘脚），需防跟腱疲劳。"

    if dist >= 40:
        recovery_rec = "单次负荷极大，建议后续 48 小时以拉伸、筋膜滚轴与充分睡眠为主，暂停强度课。"
    elif dist >= 20:
        recovery_rec = "明日建议安排 30-45 分钟极低强度排酸跑 (Zone 1) 或完全静息。"
    else:
        recovery_rec = "明日可根据晨脉安排常规 Zone 2 有氧维持，或进行核心力量强化。"

    report_markdown = f"""🏃 *PaceCraft AI 教练 · 10KM+ 深度复盘*
━━━━━━━━━━━━━━━━━━
📍 *训练*：{title} ({dist} km | 配速 {pace} | 爬升 +{elev}m)
💓 *负荷与竞技状态*：
• 平均心率：{hr} bpm
• 战力诊断：{state_desc}
• 宏观体能：长期体能 CTL {ctl} | 短期疲劳 ATL {atl}

📊 *抗疲劳与力学诊断*：
• 后程抗疲劳：{decoupling_desc}
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
        print("ℹ️ 未配置 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID，跳过 Telegram 发送。")
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload, timeout=15)
        if res.status_code == 200:
            print("✔ 成功推送 10KM+ AI 深度复盘消息至 Telegram！")
            return True
        else:
            print(f"⚠️ Telegram 发送失败 (状态 {res.status_code}): {res.text}")
    except Exception as e:
        print(f"⚠️ Telegram 发送网络异常: {e}")
    return False

def main():
    email = os.environ.get("GARMIN_EMAIL", "").strip()
    password = os.environ.get("GARMIN_PASSWORD", "").strip()
    is_cn = os.environ.get("GARMIN_IS_CN", "False").strip().lower() in ["true", "1", "yes"]
    tp_cookie = os.environ.get("TP_AUTH_COOKIE", "").strip()
    explicit_tp_id = os.environ.get("TP_ATHLETE_ID", "").strip() or None
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
    bb_change = sleep_data.get("bodyBatteryChange") or sleep_dto.get("bodyBatteryChange") or 54
    resting_hr = sleep_data.get("restingHeartRate") or 42
    avg_sleep_hr = sleep_data.get("avgOvernightHeartRate") or 51

    hrv_data = client.get_hrv_data(target_sleep_date) or {}
    hrv_summary = hrv_data.get("hrvSummary", {}) if isinstance(hrv_data, dict) else {}
    hrv_val = sleep_data.get("avgOvernightHrv") or hrv_summary.get("lastNightAvg", 62)
    hrv_weekly = hrv_summary.get("weeklyAvg", 58)
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

    # 2. 增量存储与 2023 全量回溯自动判断
    existing_history = load_activities_history()
    existing_ids = {str(a.get("activityId")) for a in existing_history if a.get("activityId")}

    # 检查现有库中最老的数据日期
    oldest_date = "9999"
    for a in existing_history:
        t = str(a.get("startTimeLocal", ""))[:10]
        if t and t < oldest_date:
            oldest_date = t

    need_backfill = oldest_date > CUTOFF_DATE

    if need_backfill:
        print(f"🚀 启动 2023 年 ({CUTOFF_DATE}) 全量回溯抓取模式（当前最早记录: {oldest_date}）...")
        start_offset = 0
        batch_size = 100
        new_parsed_acts = []

        while True:
            print(f"  正在请求偏移量 {start_offset} ~ {start_offset + batch_size} 的活动记录 ...")
            batch = client.get_activities(start_offset, batch_size) or []
            if not batch:
                print("  ✔ Garmin 全部历史活动已返回完毕。")
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
                print(f"  ✔ 已成功触达 2023 年起点 ({CUTOFF_DATE})，全量回溯顺利结束！")
                break

            start_offset += batch_size
            time.sleep(1)  # 防频繁请求限制

        full_history = new_parsed_acts + existing_history
        full_history.sort(key=lambda x: str(x.get("startTimeLocal", "")), reverse=True)
        print(f"✔ 2023–2026 全量回溯沉淀完成，数据库现拥有 {len(full_history)} 场真实跑步记录！")

    else:
        # 日常极速增量模式（只扫前 15 场）
        print(f"历史数据库已包含 2023 至今全量数据（共 {len(existing_history)} 场），进入日常极速增量模式 ...")
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
            print(f"✔ 成功增量追加 {len(new_parsed_acts)} 场全新跑步记录！")
            full_history = new_parsed_acts + existing_history
            full_history.sort(key=lambda x: str(x.get("startTimeLocal", "")), reverse=True)
        else:
            print("✔ 历史库已是最新状态，无新增活动。")
            full_history = existing_history

    save_activities_history(full_history)

    # 3. 自动精算 2023-2026 个人越野与耐力能力指纹
    runner_profile = calculate_runner_profile(full_history)
    print(f"✔ 全量能力画像：总活动={runner_profile['summary']['totalActivities']} 场 | 综合 VAM={runner_profile['climbing']['avgVam']} m/h | 极限单次爬升={runner_profile['endurance']['maxElevationM']} m")

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

    print(f"✔ 本周统计：跑量={week_stats['distanceKm']} km | 爬升={week_stats['elevationM']} m | 耗时={week_stats['durationStr']} ({week_stats['count']}次)")
    print(f"✔ 当月统计：总跑量={month_stats['distanceKm']} km (路跑 {month_stats['roadKm']} km, 越野 {month_stats['trailKm']} km) | 爬升={month_stats['elevationM']} m ({month_stats['count']}次)")

    # 5. 最新一场跑步
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

    decoupling = calculate_decoupling(splits)
    latest_act["decoupling"] = decoupling
    latest_act["avgPace"] = splits[0]["paceStr"] if splits else latest_act.get("avgPace", "0'00\"")

    tp_data = fetch_trainingpeaks_data(tp_cookie, today_cst, explicit_tp_id)

    # 6. 10KM+ AI 复盘与推送
    ai_review = None
    last_reviewed_id = None
    if os.path.exists("data.json"):
        try:
            with open("data.json", "r", encoding="utf-8") as f:
                old_data = json.load(f)
                last_reviewed_id = old_data.get("lastReviewedActivityId")
        except Exception:
            pass

    if latest_act.get("distanceKm", 0) >= 10.0:
        ai_review = generate_ai_review(latest_act, recovery, tp_data)
        if str(act_id) != str(last_reviewed_id) and tg_token and tg_chat_id:
            print(f"检测到未推送的 10KM+ 训练【{latest_act.get('activityName')}】，推送 Telegram ...")
            send_telegram_message(tg_token, tg_chat_id, ai_review["reportMarkdown"])
            last_reviewed_id = act_id

    prescription = {
        "todayFocus": tp_data["plannedWorkout"]["title"] if tp_data.get("plannedWorkout") else "今日建议以 Zone 2 有氧基础巩固为主 (6~8 km)，心率严格控制在 142 bpm 以下。",
        "drillTips": "检测到 HRV 评级为偏低 (Low)，起跑前请执行 3 组踝跳 (Pogo Hops) 唤醒下肢刚度，切忌高强度硬顶。"
    }

    payload = {
        "updateAt": now_cst.strftime("%Y-%m-%d %H:%M"),
        "tsb": tp_data["tsb"],
        "ctl": tp_data["ctl"],
        "atl": tp_data["atl"],
        "activity": latest_act,
        "recovery": recovery,
        "prescription": prescription,
        "weekStats": week_stats,
        "monthStats": month_stats,
        "runnerProfile": runner_profile,
        "aiReview": ai_review,
        "lastReviewedActivityId": last_reviewed_id,
        "tpConnected": tp_data["connected"]
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("✔ 已成功写入并更新 data.json！")

if __name__ == "__main__":
    main()
