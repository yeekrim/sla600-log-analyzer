"""SLA 600 로그 시각화

SLA 600 3D 프린터의 공정 로그에서 축별(빌드 플레이트 / 리코터 / 레진 블럭)
위치와 레진 레벨을 추출하여 시계열 그래프로 시각화한다.

특징
    1. 함수화       - parse_log / extract_series / plot_axis / plot_dashboard 로 로직 분리
    2. 설정 기반    - 필터 키워드와 정규식을 CONFIG(TIMESTAMP, AXES) 한 곳에 모아,
                      로그 포맷이 바뀌어도 코드가 아닌 설정만 수정하면 된다.
    3. 통합 대시보드 - 모든 축을 하나의 그림으로 합쳐 공정 전체를 한눈에 확인

사용 예
    python sla_movement_graph.py /path/to/your_log_file.log
    python sla_movement_graph.py your.log --save out.png        # 파일로 저장
    python sla_movement_graph.py your.log --individual          # 축별 개별 그래프
"""

import argparse
import os
import re
from datetime import datetime

import matplotlib.pyplot as plt
import pandas as pd



# 타임스탬프 정의
TIMESTAMP = {
    "regex": r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}",
    "format": "%Y-%m-%d %H:%M:%S.%f",
}

# 축별 파싱 정의
#   filters : 라인 선별 키워드 (하나라도 포함되면 대상 라인)
#   values  : 추출할 컬럼별 규칙. 각 규칙은 위에서부터 순서대로 시도한다.
#       when  : 해당 문자열이 라인에 있을 때만 적용 (생략 가능)
#       regex : 값 추출 정규식 (첫 번째 캡처 그룹이 숫자)
#       const : 정규식 대신 고정값 사용 (예: Go Origin -> 0)
AXES = [
    {
        "key": "axis0",
        "title": "Axis 0 (빌드 플레이트)",
        "ylabel": "Position",
        "filters": ["Axis 0", "Elevator"],
        "values": {
            "position": [
                {"when": "Abs Pos", "regex": r"Abs Pos:\s*([-\d\.]+)"},
                {"when": "Go Origin", "const": 0.0},
            ],
        },
    },
    {
        "key": "axis1",
        "title": "Axis 1 (리코터)",
        "ylabel": "Position",
        "filters": ["Axis 1", "Recoater"],
        "values": {
            "position": [
                {"when": "Current Pos", "regex": r"Current Pos\s+(-?[\d\.]+)"},
                {"when": "Go Origin", "const": 0.0},
            ],
        },
    },
    {
        "key": "axis2",
        "title": "Axis 2 (레진 블럭)",
        "ylabel": "Position",
        "filters": ["Before", "After"],
        "values": {
            "position": [
                {"when": "Resin current position is",
                 "regex": r"Resin current position is\s+([-\d\.]+)"},
            ],
        },
    },
    {
        "key": "resin_level",
        "title": "Resin Level (현재 vs 타겟)",
        "ylabel": "Level",
        "filters": ["Before", "After"],
        "values": {
            "Current Level": [
                {"when": "Resin current position is",
                 "regex": r"current level pos is\s+([-\d\.]+)"},
            ],
            "Target Level": [
                {"regex": r"target level is\s+([-\d\.]+)"},
            ],
        },
    },
]



# 레포에 함께 배포하는 한글 폰트 (fonts/NanumGothic-Regular.ttf, OFL 라이선스).
# 실행 환경에 한글 폰트가 없어도 그래프 라벨이 동일하게 렌더링되도록 번들한다.
BUNDLED_FONT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fonts", "NanumGothic-Regular.ttf")


def setup_korean_font():
    """한글 라벨이 깨지지 않도록 matplotlib 폰트를 설정한다.

    레포에 번들된 NanumGothic 을 우선 등록하고, 파일이 없으면 시스템에
    설치된 한글 폰트를 탐색해 사용한다. 둘 다 없으면 조용히 넘어간다
    (그래프 자체는 정상 생성되며 한글 라벨만 깨져 보일 수 있음).
    """
    from matplotlib import font_manager

    plt.rcParams["axes.unicode_minus"] = False  # 음수 부호 깨짐 방지

    # 1) 레포 번들 폰트 우선
    if os.path.exists(BUNDLED_FONT):
        font_manager.fontManager.addfont(BUNDLED_FONT)
        plt.rcParams["font.family"] = font_manager.FontProperties(
            fname=BUNDLED_FONT).get_name()
        return

    # 2) 시스템 설치 폰트로 폴백
    candidates = ["AppleGothic", "Malgun Gothic", "NanumGothic",
                  "Noto Sans CJK KR", "Noto Sans KR"]
    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in candidates:
        if name in available:
            plt.rcParams["font.family"] = name
            break


def parse_log(path, encoding="utf-8"):
    """로그 파일을 읽어 라인 리스트를 반환한다."""
    with open(path, "r", encoding=encoding) as f:
        return f.readlines()


def _extract_value(line, rules):
    """규칙 목록을 위에서부터 시도해 첫 번째로 매칭되는 값을 반환한다."""
    for rule in rules:
        when = rule.get("when")
        if when and when not in line:
            continue
        if "const" in rule:
            return rule["const"]
        m = re.search(rule["regex"], line)
        if m:
            return float(m.group(1))
    return None


def extract_series(lines, axis_cfg, ts_cfg=TIMESTAMP):
    """설정에 따라 라인에서 (시간, 값들)을 추출해 DataFrame으로 반환한다.

    한 라인은 정의된 모든 값 컬럼이 추출될 때만 하나의 행으로 기록된다.
    """
    rows = []
    for line in lines:
        if not any(kw in line for kw in axis_cfg["filters"]):
            continue

        tmatch = re.search(ts_cfg["regex"], line)
        if not tmatch:
            continue
        t = datetime.strptime(tmatch.group(), ts_cfg["format"])

        row = {"time": t}
        complete = True
        for col, rules in axis_cfg["values"].items():
            value = _extract_value(line, rules)
            if value is None:
                complete = False
                break
            row[col] = value

        if complete:
            rows.append(row)

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("time").reset_index(drop=True)
    return df


def plot_axis(df, title, ylabel="Position", ax=None, prefix=""):
    """단일 축(또는 다중 시리즈) 시계열 그래프를 그린다."""
    standalone = ax is None
    if standalone:
        _, ax = plt.subplots(figsize=(12, 5))

    value_cols = [c for c in df.columns if c != "time"]
    full_title = f"{prefix} {title}".strip()

    if df.empty:
        ax.set_title(f"{full_title} (데이터 없음)")
        ax.text(0.5, 0.5, "no data", ha="center", va="center",
                transform=ax.transAxes, color="gray")
    else:
        for col in value_cols:
            ax.plot(df["time"], df[col], label=col)
        ax.set_title(full_title)
        if len(value_cols) > 1:
            ax.legend()

    ax.set_xlabel("Time")
    ax.set_ylabel(ylabel)
    ax.grid(True)
    ax.tick_params(axis="x", rotation=45)

    if standalone:
        plt.tight_layout()
    return ax


def plot_dashboard(datasets, prefix="", suptitle="SLA 600 Movement Dashboard"):
    """여러 축을 세로로 쌓아 하나의 대시보드로 통합해 그린다.

    datasets: [(df, axis_cfg), ...]
    """
    n = len(datasets)
    fig, axes = plt.subplots(n, 1, figsize=(13, 4 * n))
    if n == 1:
        axes = [axes]

    for ax, (df, cfg) in zip(axes, datasets):
        plot_axis(df, cfg["title"], cfg.get("ylabel", "Position"),
                  ax=ax, prefix=prefix)

    fig.suptitle(f"{prefix} {suptitle}".strip(), fontsize=15, y=1.0)
    fig.tight_layout()
    return fig



# 실행

def main():
    parser = argparse.ArgumentParser(
        description="SLA 600 로그를 파싱해 축별 시계열 그래프를 그린다.")
    parser.add_argument("log_file", help="분석할 SLA 600 로그 파일 경로")
    parser.add_argument("--individual", action="store_true",
                        help="통합 대시보드 대신 축별 개별 그래프를 그린다.")
    parser.add_argument("--save", metavar="PATH",
                        help="화면 표시 대신 이미지 파일로 저장한다. "
                             "--individual 과 함께 쓰면 축별로 접미사가 붙는다.")
    args = parser.parse_args()

    setup_korean_font()
    log_lines = parse_log(args.log_file)
    # 파일명 끝 숫자를 라벨로 사용 (예: 20250403-010249-163.log -> '163')
    prefix = os.path.splitext(os.path.basename(args.log_file))[0].split("-")[-1]

    # 축별 시계열 추출
    series = {cfg["key"]: extract_series(log_lines, cfg) for cfg in AXES}
    for cfg in AXES:
        print(f"[{cfg['key']}] {cfg['title']}: {len(series[cfg['key']])} rows")

    if args.individual:
        for cfg in AXES:
            plot_axis(series[cfg["key"]], cfg["title"],
                      cfg.get("ylabel", "Position"), prefix=prefix)
            if args.save:
                root, ext = os.path.splitext(args.save)
                out = f"{root}_{cfg['key']}{ext or '.png'}"
                plt.savefig(out, dpi=150, bbox_inches="tight")
                print(f"saved: {out}")
        if not args.save:
            plt.show()
    else:
        datasets = [(series[cfg["key"]], cfg) for cfg in AXES]
        fig = plot_dashboard(datasets, prefix=prefix)
        if args.save:
            fig.savefig(args.save, dpi=150, bbox_inches="tight")
            print(f"saved: {args.save}")
        else:
            plt.show()


if __name__ == "__main__":
    main()
