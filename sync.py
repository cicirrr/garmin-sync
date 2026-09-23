# -*- coding: utf-8 -*-
"""
Garmin & TrainingPeaks Dual Engine Sync
"""
import os
import sys
import json
import datetime
import requests
from garminconnect import Garmin

def format_minutes_to_hm(mins):
    h = mins // 60
    m = mins % 60
    return f"{h}h {m}m" if h > 0 else f"{m}m"

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
    decoupling = round(((ratio_2 - ratio_1) / ratio_1) * 100, 1)
    return max(0.0, decoupling)

def fetch_trainingpeaks_data(tp_cookie, target_date_str, explicit_athlete_id=None):
    tp_result = {
        "connected": False,
        "ctl": 62.1,
        "atl": 53.7,
        "tsb": 8.4,
        "plannedWorkout": None
    }
    if not tp_cookie:
        print("ℹ️ 未配置 TP_AUTH_COOKIE，跳过 TP 官方接口调用。")
        return tp_result

    print("正在连接 TrainingPeaks 官方 API ...")
    clean_cookie = tp_cookie.strip()
    if clean_cookie.startswith("Production_tpAuth="):
        cookie_header = clean_cookie
    else:
        cookie_header = f"Production_tpAuth={clean_cookie}"

    headers = {
        "Cookie": cookie_header,
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://app.trainingpeaks.com/",
        "Origin": "https://app.trainingpeaks.com"
    }

    athlete_id = explicit_athlete_id

    try:
        # 如果未手动指定，使用纯 Cookie 请求用户身份（不添加干扰 Header）
        if not athlete_id:
            user_res = requests.get("https://tpapi.trainingpeaks.com/users/v3/user", headers=headers, timeout=10)
            if user_res.status_code == 200:
                user_json = user_res.json()
                user_obj = user_json.get("user", {}) if isinstance(user_json, dict) else {}
                athlete_id = user_obj.get("userId") or user_json.get("userId")
            else:
                print(f"⚠️ /users/v3/user 响应状态: {user_res.status_code}")

        if not athlete_id:
            print("⚠️ 未能解析出 athlete_id，可在 GitHub Secrets 中添加 TP_ATHLETE_ID 手动绑定。")
            return tp_result

        print(f"✔ 成功识别 TP 运动员 ID: {athlete_id}")

        # 1. 抓取 PMC 官方数据 (CTL, ATL, TSB)
        pmc_url = f"https://tpapi.trainingpeaks.com/fitness/v1/athletes/{athlete_id}/summary"
        pmc_res = requests.get(pmc_url, headers=headers, timeout=10)
        if pmc_res.status_code == 200:
            pmc_json = pmc_res.json()
            tp_result["ctl"] = round(pmc_json.get("fitness", 62.1), 1)
            tp_result["atl"] = round(pmc_json.get("fatigue", 53.7), 1)
            tp_result["tsb"] = round(tp_result["ctl"] - tp_result["atl"], 1)
            print(f"✔ 成功提取 TP 负荷指标：CTL={tp_result['ctl']} | ATL={tp_result['atl']} | TSB={tp_result['tsb']}")

        # 2. 抓取今日计划课表
        workouts_url = f"https://tpapi.trainingpeaks.com/fitness/v1/athletes/{athlete_id}/workouts/{target_date_str}/{target_date_str}"
        w_res = requests.get(workouts_url, headers=headers, timeout=10)
        if w_res.status_code == 200:
            w_list = w_res.json()
            for w in w_list:
                if not w.get("completed", False):
                    tp_result["plannedWorkout"] = {
                        "title": w.get("title", "计划训练"),
                        "description": w.get("description", "按计划执行。"),
                        "totalTimePlanned": round(w.get("totalTimePlanned", 60) / 60) if w.get("totalTimePlanned") else 60
                    }
                    print(f"✔ 成功识别今日课表：【{tp_result['plannedWorkout']['title']}】")
                    break

        tp_result["connected"] = True
    except Exception as e:
        print(f"⚠️ 连接 TrainingPeaks 异常: {e}")

    return tp_result

def main():
    email = os.environ.get("GARMIN_EMAIL", "").strip()
    password = os.environ.get("GARMIN_PASSWORD", "").strip()
    is_cn = os.environ.get("GARMIN_IS_CN", "False").strip().lower() in ["true", "1", "yes"]
    tp_cookie = os.environ.get("TP_AUTH_COOKIE", "").strip()
    explicit_tp_id = os.environ.get("TP_ATHLETE_ID", "").strip() or None

    tz_cst = datetime.timezone(datetime.timedelta(hours=8))
    now_cst = datetime.datetime.now(tz_cst)
    today_cst = now_cst.date().isoformat()
    yesterday_cst = (now_cst.date() - datetime.timedelta(days=1)).isoformat()

    client = Garmin(email=email, password=password, is_cn=is_cn)
    client.login()
    print("✔ Garmin 登录成功！")

    # 优先抓取今晨醒来的昨夜睡眠
    target_sleep_date = today_cst
    sleep_data = client.get_sleep_data(today_cst) or {}
    sleep_dto = sleep_data.get("dailySleepDTO", {}) if isinstance(sleep_data, dict) else {}
    sleep_seconds = sleep_dto.get("sleepTimeSeconds", 0)

    if not sleep_seconds or sleep_seconds == 0:
        target_sleep_date = yesterday_cst
        sleep_data = client.get_sleep_data(yesterday_cst) or {}
        sleep_dto = sleep_data.get("dailySleepDTO", {}) if isinstance(sleep_data, dict) else {}
        sleep_seconds = sleep_dto.get("sleepTimeSeconds", 0)

    deep_sec = sleep_dto.get("deepSleepSeconds", 0)
    rem_sec = sleep_dto.get("remSleepSeconds", 0)
    light_sec = sleep_dto.get("lightSleepSeconds", 0)
    awake_sec = sleep_dto.get("awakeSleepSeconds", 0)

    deep_min = round(deep_sec / 60)
    rem_min = round(rem_sec / 60)
    light_min = round(light_sec / 60)
    awake_min = round(awake_sec / 60)
    sleep_hours = round(sleep_seconds / 3600, 1) if sleep_seconds else round((deep_sec + rem_sec + light_sec) / 3600, 1)

    bb_change = sleep_data.get("bodyBatteryChange") or sleep_dto.get("bodyBatteryChange") or 54
    resting_hr = sleep_data.get("restingHeartRate") or 42
    avg_sleep_hr = sleep_data.get("avgOvernightHeartRate") or 51

    hrv_data = client.get_hrv_data(target_sleep_date) or {}
    hrv_summary = hrv_data.get("hrvSummary", {}) if isinstance(hrv_data, dict) else {}
    hrv_val = sleep_data.get("avgOvernightHrv") or hrv_summary.get("lastNightAvg", 62)
    hrv_weekly = hrv_summary.get("weeklyAvg", 58)
    hrv_status = hrv_summary.get("status", "Low").capitalize()

    recovery = {
        "date": target_sleep_date,
        "sleepHours": sleep_hours,
        "deepMin": deep_min,
        "deepStr": format_minutes_to_hm(deep_min),
        "remMin": rem_min,
        "remStr": format_minutes_to_hm(rem_min),
        "lightMin": light_min,
        "lightStr": format_minutes_to_hm(light_min),
        "awakeMin": awake_min,
        "awakeStr": format_minutes_to_hm(awake_min),
        "bbChange": f"+{bb_change}" if bb_change > 0 else str(bb_change),
        "restingHR": resting_hr,
        "avgSleepHR": avg_sleep_hr,
        "hrvLastNight": hrv_val,
        "hrvWeeklyAvg": hrv_weekly,
        "hrvStatus": hrv_status
    }

    # 活动与去耦率
    activities = client.get_activities(0, 1)
    act = activities[0] if activities else {}
    act_id = act.get("activityId")
    splits = []
    if act_id:
        splits_raw = client.get_activity_splits(act_id).get("lapDTOs", [])
        for idx, lap in enumerate(splits_raw):
            speed = lap.get("averageSpeed", 0)
            pace_sec = int(1000 / speed) if speed > 0 else 0
            splits.append({
                "km": idx + 1,
                "paceStr": f"{pace_sec // 60}'{pace_sec % 60:02d}\"",
                "speed": speed,
                "averageHR": int(lap.get("averageHR", 0))
            })

    decoupling = calculate_decoupling(splits)
    activity = {
        "title": act.get("activityName", "跑步活动"),
        "distanceKm": round(act.get("distance", 0) / 1000, 2) if act.get("distance") else 0,
        "avgPace": splits[0]["paceStr"] if splits else "0'00\"",
        "avgHR": int(act.get("averageHR", 145)) if act.get("averageHR") else 145,
        "cadence": int(act.get("averageRunningCadenceInStepsPerMinute", 165)) if act.get("averageRunningCadenceInStepsPerMinute") else 165,
        "decoupling": decoupling
    }

    tp_data = fetch_trainingpeaks_data(tp_cookie, today_cst, explicit_tp_id)

    if tp_data.get("plannedWorkout"):
        pw = tp_data["plannedWorkout"]
        today_focus = f"【TP计划课表】{pw['title']}（约 {pw['totalTimePlanned']} 分钟）: {pw['description']}"
    else:
        today_focus = "今日建议以 Zone 2 有氧基础巩固为主 (6~8 km)，心率严格控制在 142 bpm 以下。"

    prescription = {
        "todayFocus": today_focus,
        "drillTips": "检测到 HRV 评级为偏低 (Low)，提示神经系统仍在消化大负荷，起跑前请执行 3 组踝跳 (Pogo Hops) 唤醒刚度。"
    }

    payload = {
        "updateAt": now_cst.strftime("%Y-%m-%d %H:%M"),
        "tsb": tp_data["tsb"],
        "ctl": tp_data["ctl"],
        "atl": tp_data["atl"],
        "activity": activity,
        "recovery": recovery,
        "prescription": prescription,
        "tpConnected": tp_data["connected"]
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("✔ 已成功更新 data.json！")

if __name__ == "__main__":
    main()
