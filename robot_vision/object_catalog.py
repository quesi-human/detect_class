"""현재 YOLO11 COCO 80 클래스와 음성 명령의 한국어 별칭."""

import re

CLASS_ALIASES = {
    "person": "사람|인간|사람들",
    "bicycle": "자전거",
    "car": "자동차|승용차|차량",
    "motorcycle": "오토바이|모터사이클",
    "airplane": "비행기|항공기",
    "bus": "버스",
    "train": "기차|열차",
    "truck": "트럭",
    "boat": "보트|선박|배",
    "traffic light": "신호등",
    "fire hydrant": "소화전",
    "stop sign": "정지 표지판|정지표지판",
    "parking meter": "주차 미터기|주차미터기|주차 요금계",
    "bench": "벤치",
    "bird": "새|새들",
    "cat": "고양이",
    "dog": "개|강아지",
    "horse": "말",
    "sheep": "양",
    "cow": "소|젖소",
    "elephant": "코끼리",
    "bear": "곰",
    "zebra": "얼룩말",
    "giraffe": "기린",
    "backpack": "배낭|백팩|책가방",
    "umbrella": "우산",
    "handbag": "핸드백|손가방",
    "tie": "넥타이",
    "suitcase": "캐리어|여행 가방|여행가방",
    "frisbee": "프리스비|원반",
    "skis": "스키",
    "snowboard": "스노보드|스노우보드",
    "sports ball": "공|스포츠 공|축구공|농구공|야구공|테니스공",
    "kite": "연",
    "baseball bat": "야구 방망이|야구방망이|야구 배트",
    "baseball glove": "야구 글러브|야구글러브|야구 장갑",
    "skateboard": "스케이트보드",
    "surfboard": "서핑보드|서핑 보드",
    "tennis racket": "테니스 라켓|테니스라켓",
    "bottle": "병|물병|페트병",
    "wine glass": "와인잔|와인 잔",
    "cup": "컵|머그컵|머그잔",
    "fork": "포크",
    "knife": "칼|나이프",
    "spoon": "숟가락|스푼",
    "bowl": "그릇|사발",
    "banana": "바나나",
    "apple": "사과",
    "sandwich": "샌드위치",
    "orange": "오렌지",
    "broccoli": "브로콜리",
    "carrot": "당근",
    "hot dog": "핫도그",
    "pizza": "피자",
    "donut": "도넛|도너츠",
    "cake": "케이크|케익",
    "chair": "의자|체어",
    "couch": "소파|쇼파",
    "potted plant": "화분|화분 식물",
    "bed": "침대",
    "dining table": "식탁|테이블|책상",
    "toilet": "변기",
    "tv": "텔레비전|텔레비젼|티비|모니터",
    "laptop": "노트북|랩톱",
    "mouse": "마우스",
    "remote": "리모컨|리모콘",
    "keyboard": "키보드",
    "cell phone": "휴대폰|핸드폰|스마트폰|휴대전화",
    "microwave": "전자레인지|전자렌지",
    "oven": "오븐",
    "toaster": "토스터|토스터기",
    "sink": "싱크대|세면대",
    "refrigerator": "냉장고",
    "book": "책|서적",
    "clock": "시계",
    "vase": "꽃병",
    "scissors": "가위",
    "teddy bear": "곰인형|곰 인형|테디베어",
    "hair drier": "헤어드라이어|헤어 드라이어|드라이기",
    "toothbrush": "칫솔",
}
SUPPORTED_OBJECTS = frozenset(CLASS_ALIASES)

# 긴 명칭이 짧은 명칭과 겹치면 긴 명칭을 우선한다(꽃병/병, 곰인형/곰).
_PARTICLE_END = r"(?=$|[^가-힣]|(?:을|를|이|가|은|는|만|도|에|으로|로|과|와|랑|하고)(?=$|[^가-힣]|따라|찾|봐|보여|추적)|따라|찾|봐|보여|추적)"
_PATTERNS = []
for name, aliases in CLASS_ALIASES.items():
    for alias in [name, *aliases.split("|")]:
        pattern = (
            r"(?<![a-z0-9_])" + re.escape(alias) + r"(?![a-z0-9_])"
            if alias.isascii()
            else re.escape(alias) + _PARTICLE_END
        )
        _PATTERNS.append((name, re.compile(pattern, re.IGNORECASE)))


def mentioned_objects(text):
    matches = [
        (m.start(), m.end(), name) for name, pattern in _PATTERNS for m in pattern.finditer(text)
    ]
    used, found = [], set()
    for start, end, name in sorted(matches, key=lambda m: (-(m[1] - m[0]), m[0])):
        if any(start < b and end > a for a, b in used):
            continue
        used.append((start, end))
        found.add(name)
    return found


_SHORT_LABELS = {
    "traffic light": "TRAFFIC LT",
    "fire hydrant": "HYDRANT",
    "parking meter": "PARK METER",
    "sports ball": "BALL",
    "baseball bat": "BAT",
    "baseball glove": "GLOVE",
    "tennis racket": "RACKET",
    "potted plant": "PLANT",
    "dining table": "TABLE",
    "cell phone": "PHONE",
    "refrigerator": "FRIDGE",
    "teddy bear": "TEDDY",
}


def display_label(name):
    return _SHORT_LABELS.get(name, name.upper())
