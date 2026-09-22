# -*- coding: utf-8 -*-
import os
import json
import datetime
import requests
from garminconnect import Garmin

# 1. 从加密环境变量中读取凭据
GARMIN_EMAIL = os.environ.get("GARMIN_EMAIL")
GARMIN_PASSWORD = os.environ.get("GARMIN_PASSWORD")
IS_CN = os.environ.get("GARMIN_IS_CN", "true").lower() == "true"
WX_APPID = os.environ.get("WX_APPID")
WX_APPSECRET = os.environ.get("WX_APPSECRET")
WX_ENV_ID = os.environ.get("WX_ENV_ID")

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

def update_wechat_cloud_database(payload):
    """通过微信官方 HTTP 接口将数据写入云数据库"""
    print("正在向微信云开发写入数据...")
    # 1. 获取微信 AccessToken
    token_url = f"https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={WX_APPID}&secret={WX_APPSECRET}"
    token_res = requests.get(token_url).json()
    access_token = token_res.get("access_token")
    if not access_token:
        raise Exception(f"获取微信 AccessToken 失败: {token_res}")

    # 2. 将数据写入集合 garmin_dashboard 中的 latest_dashboard 记录
    db_url = f"https://api.weixin.qq.com/tcb/databaseupdate?access_token={access_token}"
    json_str = json.dumps(payload, ensure_ascii=False)
    query_cmd = f"db.collection('garmin_dashboard').doc('latest_dashboard').set({{data: {json_str}}})"
    
    res = requests.post(db_url, json={"env": WX_ENV_ID, "query": query_cmd}).json()
    print("微信数据库更新结果：", res)

def main():
    print("开始拉取 Garmin 健康数据...")
    today = datetime.date.today().isoformat()
    client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD, is_cn=IS_CN)
    client.login()

    # 1. 提取生理恢复指标
    sleep_raw = client.get_sleep_data(today).get("dailySleepDTO", {})
    bb_list = client.get_body_battery(today)
    today_bb = bb_list[-1] if bb_list else {}
    hrv_data = client.get_hrv_data(today) or {}

    recovery = {
        "sleepHours": round(sleep_raw.get("sleepTimeSeconds", 0) / 3600, 1),
        "deepSleep": round(sleep_raw.get("deepSleepSeconds", 0) / 60),
        "remSleep": round(sleep_raw.get("remSleepSeconds", 0) / 60),
        "lightSleep": round(sleep_raw.get("lightSleepSeconds", 0) / 60),
        "awakeSleep": round(sleep_raw.get("awakeSleepSeconds", 0) / 60),
        "hrvLastNight": hrv_data.get("hrvSummary", {}).get("lastNightAvg", 60),
        "hrvWeeklyAvg": hrv_data.get("hrvSummary", {}).get("weeklyAvg", 55),
        "hrvStatus": hrv_data.get("hrvSummary", {}).get("status", "BALANCED"),
        "batteryCharged": today_bb.get("chargedValue", 70),
        "batteryDrained": today_bb.get("drainedValue", 30),
    }

    # 2. 提取最近一次跑步活动
    activities = client.get_activities(0, 1)
    act = activities[0] if activities else {}
    act_id = act.get("activityId")
    
    splits_raw = client.get_activity_splits(act_id).get("lapDTOs", []) if act_id else []
    splits = []
    for idx, lap in enumerate(splits_raw):
        speed = lap.get("averageSpeed", 0)
        pace_sec = int(1000 / speed) if speed > 0 else 0
        pace_str = f"{pace_sec // 60}'{pace_sec % 60:02d}\""
        splits.append({
            "km": idx + 1,
            "paceStr": pace_str,
            "speed": speed,
            "averageHR": int(lap.get("averageHR", 0))
        })

    decoupling = calculate_decoupling(splits)

    activity = {
        "title": act.get("activityName", "恢复训练跑"),
        "startTime": act.get("startTimeLocal", ""),
        "distanceKm": round(act.get("distance", 0) / 1000, 2),
        "avgPace": splits[0]["paceStr"] if splits else "5'16\"",
        "avgHR": int(act.get("averageHR", 0)),
        "maxHR": int(act.get("maxHR", 0)),
        "cadence": int(act.get("averageRunningCadenceInStepsPerMinute", 180)),
        "verticalOscillation": round(act.get("avgVerticalOscillation", 8.0), 2),
        "groundContactTime": int(act.get("avgGroundContactTime", 240)),
        "strideLength": round(act.get("avgStrideLength", 100) / 100, 2),
        "verticalRatio": round(act.get("avgVerticalRatio", 7.8), 2),
        "decoupling": decoupling,
        "splits": splits
    }

    # 3. 晨间智能训练建议
    prescription = {
        "tomorrowPlan": "明日·轻松跑（有氧基础）：6~8 km，保持步频≥174。",
        "runningDrill": "A-Skip 垫步 2x20m、后踢腿 2x20m、跨步 Strides 4x80m",
        "strengthDrill": "踝跳 Pogo Hops 3x20次、台阶提踵 3x15次"
    }

    payload = {
        "_id": "latest_dashboard",
        "updateAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "activity": activity,
        "recovery": recovery,
        "prescription": prescription
    }

    # 4. 同步至微信云开发
    update_wechat_cloud_database(payload)

if __name__ == "__main__":
    main()
