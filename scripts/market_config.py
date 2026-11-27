from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKET_DATA_DIR = ROOT / "data" / "market"
DAILY_DIR = MARKET_DATA_DIR / "daily"
HOURLY_DIR = MARKET_DATA_DIR / "hourly"
MIN30_DIR = MARKET_DATA_DIR / "min30"
WEB_DIR = ROOT / "web"

FAMILIES = [
    ("油脂油料", [("A", "豆一"), ("B", "豆二"), ("M", "豆粕"), ("Y", "豆油"), ("RM", "菜粕"), ("OI", "菜油"), ("P", "棕榈油"), ("PK", "花生")]),
    ("玉米/饲料", [("C", "玉米"), ("CS", "玉米淀粉"), ("LH", "生猪")]),
    ("棉花", [("CF", "棉花")]),
    ("白糖", [("SR", "白糖")]),
    ("苹果", [("AP", "苹果")]),
    ("鸡蛋", [("JD", "鸡蛋")]),
    ("钢铁", [("I", "铁矿石"), ("J", "焦炭"), ("JM", "焦煤"), ("RB", "螺纹钢"), ("HC", "热卷"), ("SS", "不锈钢"), ("SF", "硅铁"), ("SM", "锰硅")]),
    ("能源化工", [("SC", "原油"), ("FU", "燃料油"), ("LU", "低硫燃料油"), ("BU", "沥青"), ("PG", "液化石油气"), ("L", "聚乙烯"), ("V", "聚氯乙烯"), ("PP", "聚丙烯"), ("EG", "乙二醇"), ("EB", "苯乙烯"), ("TA", "PTA"), ("PX", "对二甲苯"), ("PR", "瓶片")]),
    ("煤化工", [("MA", "甲醇"), ("UR", "尿素"), ("SH", "烧碱")]),
    ("橡胶", [("RU", "天然橡胶"), ("BR", "丁二烯橡胶")]),
    ("玻璃", [("FG", "玻璃"), ("SA", "纯碱")]),
    ("铝", [("AL", "铝"), ("AO", "氧化铝")]),
    ("镍", [("NI", "镍")]),
    ("铜", [("CU", "铜")]),
    ("锌", [("ZN", "锌")]),
    ("铅", [("PB", "铅")]),
    ("锡", [("SN", "锡")]),
    ("贵金属", [("AU", "黄金"), ("AG", "白银")]),
    ("林纸", [("SP", "纸浆"), ("LG", "原木")]),
    ("工业硅/光伏", [("SI", "工业硅"), ("PS", "多晶硅")]),
    ("碳酸锂/电池", [("LC", "碳酸锂")]),
    ("集运航运", [("EC", "集运指数")]),
    ("金融期货", [("IF", "沪深300"), ("IC", "中证500"), ("IH", "上证50"), ("IM", "中证1000")]),
]

PRODUCTS = []
for family, items in FAMILIES:
    for code, name in items:
        symbol = f"{code}0"
        PRODUCTS.append({"code": code, "name": name, "symbol": symbol, "family": family})

STEEL_CODES = ["I", "RB", "HC", "SF", "SM"]

MAIN_MONTH_WINDOW = 6
