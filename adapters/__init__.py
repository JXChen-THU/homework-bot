from . import thuoj
from . import tyche
from . import learn
from . import yuketang
from . import smartestu

# 注册表：type 名字 → 对应模块
# scraper.py 会根据 sites.yaml 里的 type 字段，从这里找抓取器
REGISTRY = {
    "thuoj": thuoj,
    "tyche": tyche,
    "learn": learn,
    "yuketang": yuketang,
    "smartestu": smartestu,
}