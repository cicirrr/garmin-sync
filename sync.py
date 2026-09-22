# -*- coding: utf-8 -*-
"""
Garmin to WeChat Cloud Sync Engine (Diagnostic Edition)
包含完整的 7 步分段自检、错误定位与脱敏日志打印
"""

import os
import sys
import json
import traceback
import datetime
import requests
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
    GarminConnectConnectionError
)

def print_separator(title=""):
    print("\n" + "=" * 30 + f" {title} " + "=" * 30)

def calculate_decoupling(splits):
    """计算有氧去耦率 (心率/配速比漂移)"""
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

def main():
    print_separator("【步骤 1/7】检查环境变量与基础配置")
    
    email = os.environ.get("GARMIN_EMAIL", "").strip()
    password = os.environ.get("GARMIN_PASSWORD", "").strip()
    is_cn_raw = os.environ.get("GARMIN_IS_CN", "False").strip().lower()
    is_cn = is_cn_raw in ["true", "1", "yes"]

    wx_appid = os.environ.get("WX_APPID", "").strip()
    wx_secret = os.environ.get("WX_APPSECRET", "").strip()
    wx_env_id = os.environ.get("WX_ENV_ID", "").strip()

    # 打印脱敏后的变量状态
    print(f"👉 GARMIN_EMAIL:     {'已设置 (' + email[:3] + '***@' + email.split('@')[-1] + ')' if '@' in email else ('未配置' if not email else '格式非邮箱')}")
    print(f"👉 GARMIN_PASSWORD:  {'已设置 (' + '*' * len(password) + ')' if password else '未配置'}")
    print(f"👉 GARMIN_IS_CN:     {is_cn} (解析自: '{is_cn_raw}')")
    print(f"👉 WX_APPID:         {'已设置 (' + wx_appid[:6] + '***)' if wx_appid else '未配置'}")
    print(f"👉 WX_APPSECRET:     {'已设置 (长度 ' + str(len(wx_secret)) + ')' if wx_secret else '未配置'}")
    print(f"👉 WX_ENV_ID:        {wx_env_id if wx_env_id else '未配置'}")

    if not email or not password:
        print("❌ 错误：GARMIN_EMAIL 或 GARMIN_PASSWORD 为空，请检查 GitHub Secrets 配置！")
        sys.exit(1)
    if not wx_appid or not wx_secret or not wx_env_id:
        print("❌ 错误：微信云开发环境变量不完整，请检查 WX_APPID / WX_APPSECRET / WX_ENV_ID！")
        sys.exit(1)

    print_separator("【步骤 2/7】连接 Garmin 认证中心")
    domain_desc = "中国区 (garmin.cn)" if is_cn else "国际区 (garmin.com)"
    print(f"正在初始化 Garmin 客户端，目标分区: {domain_desc} ...")
    
    try:
        client = Garmin(email=email, password=password, is_cn=is_cn)
        print("发起登录认证请求 (使用新版 0.3.x 登录流)...")
        client.login()
        print("✔ Garmin 登录成功！已获取会话授权。")
    except GarminConnectAuthenticationError as e:
        print(f"❌ 认证失败 (401 Unauthorized)：请核对 Garmin 邮箱和密码是否正确，或者账号是否需要二次验证码。")
        print(f"详细错误: {e}")
        traceback.print_exc()
        sys.exit(1)
    except GarminConnectTooManyRequestsError as e:
        print(f"❌ 触发流控 (429 Too Many Requests)：被 Garmin 服务端频率限制或防护策略拦截。")
        print(f"详细错误: {e}")
        traceback.print_exc()
        sys.exit(1)
    except GarminConnectConnectionError as e:
        print(f"❌ 网络连通性失败：无法连接到 Garmin 服务器。")
        print(f"详细错误: {e}")
        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"❌ 未知登录异常: {e}")
        traceback.print_exc()
        sys.exit(1)

    today = datetime.date.today().isoformat()
    print(f"当前查询基准日期: {today}")

    print_separator("【步骤 3/7】拉取生理恢复与睡眠指标")
    recovery = {}
    try:
        print("-> 正在请求 get_sleep_data ...")
        sleep_data = client.get_sleep_data(today) or {}
        sleep_dto = sleep_data.get("dailySleepDTO", {})
        sleep_seconds = sleep_dto.get("sleepTimeSeconds", 0)
        print(f"   睡眠记录状态: {'已获取 (' + str(round(sleep_seconds / 3600, 1)) + ' 小时)' if sleep_seconds else '无今日睡眠数据 (使用默认值)'}")

        print("-> 正在请求 get_body_battery ...")
        bb_list = client.get_body_battery(today) or []
        today_bb = bb_list[-1] if bb_list else {}
        print(f"   身体电量记录状态: {'已获取 ' + str(len(bb_list)) + ' 条数据点' if bb_list else '无今日电量数据'}")

        print("-> 正在请求 get_hrv_data ...")
        hrv_data = client.get_hrv_data(today) or {}
        hrv_summary = hrv_data.get("hrvSummary", {})
        print(f"   HRV 记录状态: 昨夜均值={hrv_summary.get('lastNightAvg', 'N/A')}, 状态={hrv_summary.get('status', 'N/A')}")

        recovery = {
            "sleepHours": round(sleep_seconds / 3600, 1) if sleep_seconds else 7.5,
            "deepSleep": round(sleep_dto.get("deepSleepSeconds", 0) / 60) if sleep_seconds else 90,
            "remSleep": round(sleep_dto.get("remSleepSeconds", 0) / 60) if sleep_seconds else 95,
            "lightSleep": round(sleep_dto.get("lightSleepSeconds", 0) / 60) if sleep_seconds else 240,
            "awakeSleep": round(sleep_dto.get("awakeSleepSeconds", 0) / 60) if sleep_seconds else 25,
            "hrvLastNight": hrv_summary.get("lastNightAvg", 60),
            "hrvWeeklyAvg": hrv_summary.get("weeklyAvg", 58),
            "hrvStatus": hrv_summary.get("status", "BALANCED"),
            "batteryCharged": today_bb.get("chargedValue", 75),
            "batteryDrained": today_bb.get("drainedValue", 35)
        }
        print("✔ 生理数据整合完成。")
    except Exception as e:
        print(f"⚠️ 拉取生理数据时出现非致命错误: {e}")
        traceback.print_exc()
        recovery = {
            "sleepHours": 7.5, "deepSleep": 90, "remSleep": 95, "lightSleep": 240, "awakeSleep": 25,
            "hrvLastNight": 60, "hrvWeeklyAvg": 58, "hrvStatus": "BALANCED",
            "batteryCharged": 75, "batteryDrained": 35
        }

    print_separator("【步骤 4/7】拉取最近跑步训练与分段数据")
    activity = {}
    try:
        print("-> 正在请求 get_activities(0, 1) ...")
        activities = client.get_activities(0, 1)
        if not activities:
            print("   未检索到任何运动活动，使用预设模板。")
            act = {}
            act_id = None
        else:
            act = activities[0]
            act_id = act.get("activityId")
            print(f"   最新活动: ID={act_id}, 名称='{act.get('activityName')}', 类型={act.get('activityType', {}).get('typeKey')}")

        splits = []
        if act_id:
            print(f"-> 正在请求 get_activity_splits({act_id}) ...")
            splits_raw = client.get_activity_splits(act_id).get("lapDTOs", [])
            print(f"   分段公里数: 共获取到 {len(splits_raw)} 个 Lap")
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
        print(f"   计算得出有氧去耦率 (心率漂移): {decoupling}%")

        activity = {
            "title": act.get("activityName", "晨间恢复跑"),
            "startTime": act.get("startTimeLocal", datetime.datetime.now().strftime("%Y-%m-%d %H:%M")),
            "distanceKm": round(act.get("distance", 0) / 1000, 2) if act.get("distance") else 8.52,
            "avgPace": splits[0]["paceStr"] if splits else "5'16\"",
            "avgHR": int(act.get("averageHR", 146)) if act.get("averageHR") else 146,
            "maxHR": int(act.get("maxHR", 162)) if act.get("maxHR") else 162,
            "cadence": int(act.get("averageRunningCadenceInStepsPerMinute", 182)) if act.get("averageRunningCadenceInStepsPerMinute") else 182,
            "verticalOscillation": round(act.get("avgVerticalOscillation", 7.8), 2) if act.get("avgVerticalOscillation") else 7.8,
            "groundContactTime": int(act.get("avgGroundContactTime", 232)) if act.get("avgGroundContactTime") else 232,
            "strideLength": round(act.get("avgStrideLength", 104) / 100, 2) if act.get("avgStrideLength") else 1.04,
            "verticalRatio": round(act.get("avgVerticalRatio", 7.5), 2) if act.get("avgVerticalRatio") else 7.5,
            "decoupling": decoupling,
            "splits": splits
        }
        print("✔ 活动与高阶跑姿数据提取完毕。")
    except Exception as e:
        print(f"⚠️ 拉取活动数据时出现非致命错误: {e}")
        traceback.print_exc()

    print_separator("【步骤 5/7】生成训练处方与大屏数据载荷")
    prescription = {
        "tomorrowPlan": "明日·轻松跑（有氧巩固）：6~8 km，严格压制心率在 Zone 2 (<145 bpm)。",
        "runningDrill": "A-Skip 垫步 2x20m、高抬腿转加速跑 3x30m、跨步冲刺 Strides 4x80m",
        "strengthDrill": "单腿硬拉 3x12次/侧、踝关节弹性跳 Pogo Hops 3x20次、台阶离心提踵 3x15次"
    }

    final_payload = {
        "_id": "latest_dashboard",
        "updateAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "activity": activity,
        "recovery": recovery,
        "prescription": prescription
    }
    print(f"数据载荷已组装，总字节数: {len(json.dumps(final_payload, ensure_ascii=False))} 字符")

    print_separator("【步骤 6/7】获取微信小程序接口凭证 (Access Token)")
    token_url = f"https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={wx_appid}&secret={wx_secret}"
    try:
        resp = requests.get(token_url, timeout=10).json()
        access_token = resp.get("access_token")
        if not access_token:
            print(f"❌ 获取微信 Access Token 失败，微信返回信息: {resp}")
            sys.exit(1)
        print(f"✔ 微信 Access Token 获取成功 (有效时长: {resp.get('expires_in')} 秒)")
    except Exception as e:
        print(f"❌ 访问微信认证接口发生网络异常: {e}")
        traceback.print_exc()
        sys.exit(1)

    print_separator("【步骤 7/7】同步写入微信云开发数据库")
    db_url = f"https://api.weixin.qq.com/tcb/databaseupdate?access_token={access_token}"
    json_str = json.dumps(final_payload, ensure_ascii=False)
    query_cmd = f"db.collection('garmin_dashboard').doc('latest_dashboard').set({{data: {json_str}}})"

    try:
        db_res = requests.post(db_url, json={"env": wx_env_id, "query": query_cmd}, timeout=15).json()
        print(f"微信云开发响应报文: {db_res}")
        if db_res.get("errcode") == 0:
            print("🎉 恭喜！数据已成功写入微信云开发数据库 (集合: garmin_dashboard, 文档: latest_dashboard)！")
        else:
            print(f"⚠️ 微信数据库返回错误码: {db_res.get('errcode')}, 信息: {db_res.get('errmsg')}")
            if db_res.get("errcode") == -502001:
                print("提示：请确认云开发控制台中是否存在名为 'garmin_dashboard' 的数据库集合！")
    except Exception as e:
        print(f"❌ 写入微信云开发发生网络异常: {e}")
        traceback.print_exc()
        sys.exit(1)

    print_separator("全流程执行完毕")

if __name__ == "__main__":
    main()
