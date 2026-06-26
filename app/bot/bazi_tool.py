"""
八字排盘工具 — 享客虾 Bot 工具集
基于出生年月日时计算四柱八字 + 十神 + 简易AI解读提示
"""
from datetime import datetime
import json

# ── 天干地支 ──
TIAN_GAN = ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛", "壬", "癸"]
DI_ZHI = ["子", "丑", "寅", "卯", "辰", "巳", "午", "未", "申", "酉", "戌", "亥"]
SHENG_XIAO = ["鼠", "牛", "虎", "兔", "龙", "蛇", "马", "羊", "猴", "鸡", "狗", "猪"]

# 五行
WU_XING_GAN = ["木", "木", "火", "火", "土", "土", "金", "金", "水", "水"]
WU_XING_ZHI = ["水", "土", "木", "木", "土", "火", "火", "土", "金", "金", "土", "水"]

# 十神（日干为基准）
SHI_SHEN_TABLE = {
    "甲": ["比肩", "劫财", "食神", "伤官", "偏财", "正财", "七杀", "正官", "偏印", "正印"],
    "乙": ["劫财", "比肩", "伤官", "食神", "正财", "偏财", "正官", "七杀", "正印", "偏印"],
    "丙": ["比肩", "劫财", "食神", "伤官", "偏财", "正财", "七杀", "正官", "偏印", "正印"],
    "丁": ["劫财", "比肩", "伤官", "食神", "正财", "偏财", "正官", "七杀", "正印", "偏印"],
    "戊": ["比肩", "劫财", "食神", "伤官", "偏财", "正财", "七杀", "正官", "偏印", "正印"],
    "己": ["劫财", "比肩", "伤官", "食神", "正财", "偏财", "正官", "七杀", "正印", "偏印"],
    "庚": ["比肩", "劫财", "食神", "伤官", "偏财", "正财", "七杀", "正官", "偏印", "正印"],
    "辛": ["劫财", "比肩", "伤官", "食神", "正财", "偏财", "正官", "七杀", "正印", "偏印"],
    "壬": ["比肩", "劫财", "食神", "伤官", "偏财", "正财", "七杀", "正官", "偏印", "正印"],
    "癸": ["劫财", "比肩", "伤官", "食神", "正财", "偏财", "正官", "七杀", "正印", "偏印"],
}

# ── 节气日期表（2020-2030年立春，用于年柱判断）──
SPRING_START = {
    2020: (2, 4), 2021: (2, 3), 2022: (2, 4), 2023: (2, 4), 2024: (2, 4),
    2025: (2, 3), 2026: (2, 4), 2027: (2, 4), 2028: (2, 4), 2029: (2, 3),
    2030: (2, 4),
}

# 月柱起始月（正月=寅，以节气分界）
# 月支固定：寅(1月)卯(2月)辰(3月)巳(4月)午(5月)未(6月)申(7月)酉(8月)戌(9月)亥(10月)子(11月)丑(12月)
MONTH_ZHI = [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 0, 1]  # 正月寅=2 ... 十二月丑=1 的 DI_ZHI 索引

# 月干公式：(年干 % 5 * 2 + 月支序数) % 10
# 年干: 甲0乙1丙2丁3戊4己5庚6辛7壬8癸9
# 月支序数: 寅0卯1辰2巳3午4未5申6酉7戌8亥9子10丑11


def _year_pillar(year, month, day):
    """计算年柱，以立春为界"""
    spring_info = SPRING_START.get(year, (2, 4))
    spring_month, spring_day = spring_info
    if month < spring_month or (month == spring_month and day < spring_day):
        year_used = year - 1
    else:
        year_used = year
    gan_idx = (year_used - 4) % 10
    zhi_idx = (year_used - 4) % 12
    return TIAN_GAN[gan_idx], DI_ZHI[zhi_idx], gan_idx, zhi_idx


def _month_pillar(year_gan_idx, month, day):
    """
    计算月柱
    月支由节气决定（简化：每月大约在4-8号换气）
    月干公式：甲己之年丙作首，乙庚之岁戊为头...
    即 (year_gan % 5 * 2) 为正月干偏移
    """
    # 简化月支：按月+节气粗略判断
    # 节气换月大致日期（公历）
    JIEQI_DAY = [6, 4, 6, 5, 6, 6, 7, 7, 8, 8, 7, 6]  # 各月节气日前后
    # 月支索引（寅=0对应公历2月左右）
    raw_zhi = (month + 1) % 12
    if day < JIEQI_DAY[month - 1]:
        raw_zhi = (month) % 12  # 还未到换气月，用上个月
    zhi_idx = raw_zhi % 12

    # 月干
    gan_idx = (year_gan_idx % 5 * 2 + zhi_idx) % 10
    return TIAN_GAN[gan_idx], DI_ZHI[zhi_idx], gan_idx, zhi_idx


def _day_pillar(year, month, day):
    """
    计算日柱
    使用儒略日或已知参考日
    参考：1900-01-01 = 甲戌日 = 天干10%10=0(甲), 地支10%12=10(戌)，cycle索引10
    """
    # 计算从1900-01-01到目标日期的天数
    d0 = datetime(1900, 1, 1)
    d1 = datetime(year, month, day)
    days = (d1 - d0).days
    cycle_idx = (10 + days) % 60  # 1900-01-01是第10位(甲戌)
    gan_idx = cycle_idx % 10
    zhi_idx = cycle_idx % 12
    return TIAN_GAN[gan_idx], DI_ZHI[zhi_idx], gan_idx, zhi_idx


def _hour_pillar(day_gan_idx, hour):
    """
    计算时柱
    时支：子(23-1)丑(1-3)寅(3-5)卯(5-7)辰(7-9)巳(9-11)午(11-13)未(13-15)申(15-17)酉(17-19)戌(19-21)亥(21-23)
    时干公式：(日干%5*2 + 时支序数)%10
    """
    zhi_idx = ((hour + 1) // 2) % 12
    gan_idx = (day_gan_idx % 5 * 2 + zhi_idx) % 10
    return TIAN_GAN[gan_idx], DI_ZHI[zhi_idx], gan_idx, zhi_idx


def _get_shi_shen(day_gan_idx, other_gan_idx):
    """获取十神"""
    gan = TIAN_GAN[day_gan_idx]
    targets = SHI_SHEN_TABLE.get(gan, SHI_SHEN_TABLE["甲"])
    return targets[other_gan_idx]


def get_bazi(year, month, day, hour=12, gender=None):
    """
    排盘主函数
    返回完整八字信息字典
    """
    # 四柱
    y_gan, y_zhi, y_gi, y_zi = _year_pillar(year, month, day)
    m_gan, m_zhi, m_gi, m_zi = _month_pillar(y_gi, month, day)
    d_gan, d_zhi, d_gi, d_zi = _day_pillar(year, month, day)
    h_gan, h_zhi, h_gi, h_zi = _hour_pillar(d_gi, hour)

    # 纳音五行（简单映射）
    bazi_str = f"{y_gan}{y_zhi}{m_gan}{m_zhi}{d_gan}{d_zhi}{h_gan}{h_zhi}"

    # 五行统计
    all_gan_zhi = [y_gi, y_zi, m_gi, m_zi, d_gi, d_zi, h_gi, h_zi]
    wx_count = {"木": 0, "火": 0, "土": 0, "金": 0, "水": 0}
    for idx in all_gan_zhi[:4]:  # 天干
        wx_count[WU_XING_GAN[idx]] = wx_count.get(WU_XING_GAN[idx], 0) + 1
    for idx in all_gan_zhi[4:]:  # 地支
        wx_count[WU_XING_ZHI[idx]] = wx_count.get(WU_XING_ZHI[idx], 0) + 1

    # 十神
    shi_shens = [
        ("年干", y_gan, _get_shi_shen(d_gi, y_gi)),
        ("月干", m_gan, _get_shi_shen(d_gi, m_gi)),
        ("日干", d_gan, "日主"),
        ("时干", h_gan, _get_shi_shen(d_gi, h_gi)),
    ]
    
    sheng_xiao = SHENG_XIAO[y_zi]

    result = {
        "bazi": bazi_str,
        "四柱": {
            "年柱": f"{y_gan}{y_zhi}",
            "月柱": f"{m_gan}{m_zhi}",
            "日柱": f"{d_gan}{d_zhi}",
            "时柱": f"{h_gan}{h_zhi}",
        },
        "天干": [y_gan, m_gan, d_gan, h_gan],
        "地支": [y_zhi, m_zhi, d_zhi, h_zhi],
        "生肖": sheng_xiao,
        "日主": d_gan,
        "五行统计": wx_count,
        "十神": shi_shens,
        "性别": gender or "未知",
    }
    return result


def format_bazi_result(data):
    """格式化为可读文本"""
    lines = []
    lines.append(f"🎯 八字排盘")
    lines.append(f"━━━━━━━━━━━━━━")
    lines.append(f"四柱：{' '.join(data['四柱'].values())}")
    lines.append(f"生肖：{data['生肖']}")
    lines.append(f"日主：{data['日主']}")
    lines.append(f"五行：{' '.join(f'{k}{v}' for k,v in data['五行统计'].items() if v>0)}")
    lines.append(f"")
    lines.append(f"📋 十神")
    for role, gan, name in data['十神']:
        lines.append(f"  {role} {gan} → {name}")
    lines.append(f"")
    lines.append(f"💡 提示：回复「排盘解读」可获取AI深度解读")
    return "\n".join(lines)


def format_hepan(p1, p2):
    """合盘：两人八字对比"""
    lines = []
    lines.append(f"💞 合盘分析")
    lines.append(f"━━━━━━━━━━━━━━")
    lines.append(f"【{p1.get('性别','A')}方】{p1['日主']}日主 · {' '.join(p1['四柱'].values())}")
    lines.append(f"【{p2.get('性别','B')}方】{p2['日主']}日主 · {' '.join(p2['四柱'].values())}")
    lines.append(f"")
    lines.append(f"五行对比：")
    for wx in ["木","火","土","金","水"]:
        a = p1['五行统计'].get(wx,0)
        b = p2['五行统计'].get(wx,0)
        lines.append(f"  {wx}：{a} vs {b}")
    lines.append(f"")
    lines.append(f"💡 回复「解读合盘」获取AI深度合盘分析")
    return "\n".join(lines)


# ── 自测 ──
if __name__ == "__main__":
    # 测试：2026-06-26 12:00
    r = get_bazi(2026, 6, 26, 12, "男")
    print(format_bazi_result(r))
    print()
    r2 = get_bazi(2014, 1, 8, 8, "男")
    print(format_bazi_result(r2))
