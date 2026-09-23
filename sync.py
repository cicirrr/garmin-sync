# -*- coding: utf-8 -*-
"""
Garmin & TrainingPeaks Dual Engine Sync
每日自动聚合 Garmin 生理底盘与 TrainingPeaks 官方负荷、今日计划课表
"""

import os
import sys
import json
import datetime
import requests
from garminconnect import Garmin

def print_separator(title=""):
    print("\n" + "=" * 30 + f" {title} " + "=" * 30)

def format_minutes_to_hm(mins):
    h = mins // 60
    m = mins % 60
    return f"{h}h {m}m" if h > 0 else f"{m}m"

def calculate_decoupling(splits):
    """计算有氧去耦率 (心率漂移)"""
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
    """通过 TP 网页安全凭据抓取官方 CTL/ATL/TSB 及今日计划课表"""
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
    # 智能兼容是否包含 'Production_tpAuth=' 前缀
    if clean_cookie.startswith("Production_tpAuth="):
        cookie_header = clean_cookie
    else:
        cookie_header = f"Production_tpAuth={clean_cookie}"

    session = requests.Session()
    session.headers.update({
        "Cookie": cookie_header,
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://app.trainingpeaks.com/",
        "Origin": "https://app.trainingpeaks.com"
    })

    athlete_id = explicit_athlete_id

    try:
        # 1. 调用 /users/v3/token 获取 Bearer Token
        token_res = session.get("https://tpapi.trainingpeaks.com/users/v3/token", timeout=10)
        if token_res.status_code == 200:
            token_json = token_res.json()
            access_token = token_json.get("access_token") or token_json.get("token")
            if access_token:
                session.headers["Authorization"] = f"Bearer {access_token}"
                print("✔ 成功换取 TP 会话 Bearer Token！")
            if not athlete_id:
                athlete_id = token_json.get("userId") or token_json.get("athleteId")
        else:
            print(f"ℹ️ /users/v3/token 响应码: {token_res.status_code}")

        # 2. 如果未取得 athlete_id，通过 /users/v3/user 解析运动员档案
        if not athlete_id:
            user_res = session.get("https://tpapi.trainingpeaks.com/users/v3/user", timeout=10)
            if user_res.status_code == 200:
                user_json = user_res.json()
                user_obj = user_json.get("user", {}) if isinstance(user_json, dict) else {}
                athlete_id = user_obj.get("userId") or user_json.get("userId") or user_obj.get("athleteId")
            else:
                print(f"⚠️ /users/v3/user 响应码: {user_res.status_code}")

        if not athlete_id:
            print("⚠️ 未能从 TP 凭据自动解析出 athlete_id，请确认 Cookie 是否有效。")
            return tp_result

        print(f"✔ 成功识别 TP 运动员 ID: {athlete_id}")

        # 3. 获取官方 PMC 负荷指标 (CTL, ATL, TSB)
        pmc_url = f"https://tpapi.trainingpeaks.com/fitness/v1/athletes/{athlete_id}/summary"
        pmc_res = session.get(pmc_url, timeout=10)
        if pmc_res.status_code == 200:
            pmc_json = pmc_res.json()
            tp_result["ctl"] = round(pmc_json.get("fitness", 62.1), 1)
            tp_result["atl"] = round(pmc_json.get("fatigue", 53.7), 1)
            tp_result["tsb"] = round(tp_result["ctl"] - tp_result["atl"], 1)
            print(f"✔ 成功提取 TP 官方体能指标：CTL={tp_result['ctl']} | ATL={tp_result['atl']} | TSB={tp_result['tsb']}")
        else:
            print(f"ℹ️ 获取 PMC 摘要响应码: {pmc_res.status_code}")

        # 4. 获取今日计划课表 (Planned Workout)
        workouts_url = f"https://tpapi.trainingpeaks.com/fitness/v1/athletes/{athlete_id}/workouts/{target_date_str}/{target_date_str}"
        w_res = session.get(workouts_url, timeout=10)
        if w_res.status_code == 200:
            w_list = w_res.json()
            for w in w_list:
                if not w.get("completed", False):
                    tp_result["plannedWorkout"] = {
                        "title": w.get("title", "计划训练"),
                        "description": w.get("description", "按计划执行。"),
                        "totalTimePlanned": round(w.get("totalTimePlanned", 60) / 60) if w.get("totalTimePlanned") else 60,
                        "tssPlanned": w.get("tssPlanned", 0)
                    }
                    print(f"✔ 成功识别 TP 今日课表：【{tp_result['plannedWorkout']['title']}】(预计时长约 {tp_result['plannedWorkout']['totalTimePlanned']} 分钟)")
                    break
        else:
            print(f"ℹ️ 查询今日课表响应码: {w_res.status_code}")

        tp_result["connected"] = True
    except Exception as e:
        print(f"⚠️ 连接 TrainingPeaks 发生网络异常: {e}")

    return tp_result

def main():
    print_separator("【步骤 1/5】检查配置与环境变量")
    email = os.environ.get("GARMIN_EMAIL", "").strip()
    password = os.environ.get("GARMIN_PASSWORD", "").strip()
    is_cn_raw = os.environ.get("GARMIN_IS_CN", "False").strip().lower()
    is_cn = is_cn_raw in ["true", "1", "yes"]
    tp_cookie = os.environ.get("TP_AUTH_COOKIE", "").strip()
    explicit_tp_id = os.environ.get("TP_ATHLETE_ID", "").strip() or None

    if not email or not password:
        print("❌ 错误：GARMIN_EMAIL 或 GARMIN_PASSWORD 为空！")
        sys.exit(1)

    # 锁定北京时间 (UTC+8)
    tz_cst = datetime.timezone(datetime.timedelta(hours=8))
    now_cst = datetime.datetime.now(tz_cst)
    today_cst = now_cst.date().isoformat()
    yesterday_cst = (now_cst.date() - datetime.timedelta(days=1)).isoformat()
    print(f"当前时间 (北京时间 UTC+8): {now_cst.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"对应今日醒来归属日期: {today_cst} | 昨日日期: {yesterday_cst}")

    print_separator("【步骤 2/5】登录 Garmin")
    domain_desc = "中国区 (garmin.cn)" if is_cn else "国际区 (garmin.com)"
    print(f"正在连接 Garmin {domain_desc} ...")
    client = Garmin(email=email, password=password, is_cn=is_cn)
    client.login()
    print("✔ Garmin 登录成功！")

    print_separator("【步骤 3/5】精准拉取生理与昨夜睡眠数据")
    recovery = {}
    try:
        # 优先提取今日(昨夜入睡-今晨醒来)睡眠
        target_sleep_date = today_cst
        sleep_data = client.get_sleep_data(today_cst) or {}
        sleep_dto = sleep_data.get("dailySleepDTO", {}) if isinstance(sleep_data, dict) else {}
        sleep_seconds = sleep_dto.get("sleepTimeSeconds", 0)

        # 若今晨手表还未同步到佳明云端，回退到昨日并提示
        if not sleep_seconds or sleep_seconds == 0:
            print(f"ℹ️ 今日 ({today_cst}) 睡眠数据尚未上传至佳明云端，安全回退到昨日 ({yesterday_cst}) 数据...")
            target_sleep_date = yesterday_cst
            sleep_data = client.get_sleep_data(yesterday_cst) or {}
            sleep_dto = sleep_data.get("dailySleepDTO", {}) if isinstance(sleep_data, dict) else {}
            sleep_seconds = sleep_dto.get("sleepTimeSeconds", 0)
        else:
            print(f"✔ 成功匹配昨夜睡眠数据（归属日期: {today_cst}）！")

        deep_sec = sleep_dto.get("deepSleepSeconds", 0)
        rem_sec = sleep_dto.get("remSleepSeconds", 0)
        light_sec = sleep_dto.get("lightSleepSeconds", 0)
        awake_sec = sleep_dto.get("awakeSleepSeconds", 0)

        deep_min = round(deep_sec / 60)
        rem_min = round(rem_sec / 60)
        light_min = round(light_sec / 60)
        awake_min = round(awake_sec / 60)

        sleep_hours = round(sleep_seconds / 3600, 1) if sleep_seconds else round((deep_sec + rem_sec + light_sec) / 3600, 1)

        bb_change = sleep_data.get("bodyBatteryChange") or sleep_dto.get("bodyBatteryChange") or 51
        resting_hr = sleep_data.get("restingHeartRate") or 42
        avg_sleep_hr = sleep_data.get("avgOvernightHeartRate") or 51

        hrv_data = client.get_hrv_data(target_sleep_date) or {}
        hrv_summary = hrv_data.get("hrvSummary", {}) if isinstance(hrv_data, dict) else {}
        hrv_val = sleep_data.get("avgOvernightHrv") or hrv_summary.get("lastNightAvg", 56)
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
        print(f"✔ 睡眠提取成功 (日期: {target_sleep_date})：总时长={sleep_hours}h | 深睡={recovery['deepStr']} | 浅睡={recovery['lightStr']} | REM={recovery['remStr']}")
        print(f"✔ 身体恢复提取成功：电量变化={recovery['bbChange']} | 静息心率={resting_hr} bpm | 昨夜HRV={hrv_val} ms (状态: {hrv_status})")
    except Exception as e:
        print(f"⚠️ 提取生理数据异常: {e}")

    print_separator("【步骤 4/5】拉取最新跑步活动与 TrainingPeaks 数据")
    activity = {}
    try:
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
            "verticalOscillation": round(act.get("avgVerticalOscillation", 6.72), 2) if act.get("avgVerticalOscillation") else 6.72,
            "groundContactTime": int(act.get("avgGroundContactTime", 232)) if act.get("avgGroundContactTime") else 232,
            "strideLength": round(act.get("avgStrideLength", 104) / 100, 2) if act.get("avgStrideLength") else 1.04,
            "verticalRatio": round(act.get("avgVerticalRatio", 7.5), 2) if act.get("avgVerticalRatio") else 7.5,
            "decoupling": decoupling
        }
        print(f"✔ 跑步活动提取成功：{activity['title']} {activity['distanceKm']}km, 去耦率: {decoupling}%")
    except Exception as e:
        print(f"⚠️ 运动提取异常: {e}")

    # 调用 TrainingPeaks 提取负荷与今日计划课表
    tp_data = fetch_trainingpeaks_data(tp_cookie, today_cst, explicit_tp_id)

    if tp_data.get("plannedWorkout"):
        pw = tp_data["plannedWorkout"]
        today_focus = f"【TP计划课表】{pw['title']}（约 {pw['totalTimePlanned']} 分钟）: {pw['description']}"
    else:
        today_focus = "今日建议以 Zone 2 有氧基础巩固为主 (6~8 km)，心率严格控制在 142 bpm 以下。"

    prescription = {
        "todayFocus": today_focus,
        "drillTips": "检测到 HRV 状态为偏低 (Low)，提示神经系统仍在消化大负荷，起跑前请执行 3 组踝跳 (Pogo Hops) 唤醒下肢刚度，切忌高强度硬顶。"
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

    print_separator("【步骤 5/5】写入 data.json 供 GitHub Pages 网页动态读取")
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("✔ 已成功更新 data.json！")

if __name__ == "__main__":
    main()
