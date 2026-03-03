from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
import time
import pandas as pd
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import re
from datetime import datetime
import os
import matplotlib.pyplot as plt
import matplotlib.dates as mdates


SEARCH_URL = "https://suumo.jp/jj/chintai/ichiran/FR301FC001/?ar=030&bs=040&pc=30&smk=&po1=25&po2=99&shkr1=03&shkr2=03&shkr3=03&shkr4=03&rn=0095&ek=009530810&ek=009525080&ek=009513460&ra=014&cb=0.0&ct=9999999&md=06&md=07&ts=1&et=10&mb=0&mt=9999999&cn=9999999&tc=0400301&tc=0400101&tc=0400501&tc=0400601&tc=0400801&fw2="

# 履歴ファイルのパス
HISTORY_FILE = "/home/taki/suumo_history.csv"

options = Options()

# Chromiumのバイナリパスを指定（クロームブック用）
options.binary_location = "/usr/bin/chromium-browser"

options.add_argument("--headless=new")
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")
options.add_argument("--disable-gpu")

# ChromeDriverのパスを指定
service = Service("/usr/bin/chromedriver")

driver = webdriver.Chrome(service=service, options=options)
driver.get(SEARCH_URL)

time.sleep(5)

# 物件詳細リンク取得
links = []
items = driver.find_elements(By.CSS_SELECTOR, "a.js-cassette_link_href")
for a in items:
    href = a.get_attribute("href")
    if href and "chintai/jnc_" in href:
        links.append(href)

print(f"取得リンク数: {len(links)}")

results = []

for i, url in enumerate(links):
    print(f"{i+1}/{len(links)} 処理中...")
    driver.get(url)
    wait = WebDriverWait(driver, 10)
    wait.until(
        EC.presence_of_element_located(
            (By.TAG_NAME, "body")
        )
    )   

    try:
        name = driver.find_element(By.CLASS_NAME, "section_h1-header-title").text
    except:
        name = ""

    try:
        station = driver.find_element(By.CSS_SELECTOR, ".property_view_table tr:nth-child(1) td").text
    except:
        station = ""

    try:
        age = driver.find_element(By.XPATH, "//th[text()='築年数']/following-sibling::td").text
    except:
        age = ""

    try:
        area_elem = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located(
                (By.XPATH, "//th[contains(.,'専有面積')]/following-sibling::td")
            )
        )
        area_text = area_elem.text
        area_value = float(area_text.replace("m2","").strip())
    except:
        area_value = 0

    # パターンA
    rent_value = 0
    manage_value = 0

    # 賃料
    try:
        rent_elem = driver.find_element(
            By.XPATH,
            "//th[contains(.,'賃料')]/following-sibling::td"
        )
        rent_text = rent_elem.text
        
        # 改行で分割
        lines = rent_text.split("\n")
        
        # 1行目が賃料
        if lines:
            first_line = lines[0].strip()
            if "万円" in first_line:
                rent_value = float(first_line.replace("万円","").strip()) * 10000
            elif "円" in first_line:
                rent_value = float(first_line.replace("円","").replace(",","").strip())
        
        # 2行目以降にカッコ付きの管理費がある場合
        for line in lines[1:]:
            line = line.strip()
            if "(" in line and "円" in line:
                cleaned = line.replace("(", "").replace(")", "").replace("円", "").replace(",", "").strip()
                if cleaned and cleaned.replace(".", "").isdigit():
                    manage_value = float(cleaned)
                    break

    except Exception as e:
        rent_value = 0

    # 管理費
    if manage_value == 0:
        try:
            manage_elem = driver.find_element(
                By.XPATH,
                "//th[contains(.,'管理費') or contains(.,'共益費')]/following-sibling::td"
            )
            manage_text = manage_elem.text

            lines = manage_text.split("\n")

            for line in lines:
                raw = line.strip()
                
                if raw in ["-", "なし", "込み", "込"]:
                    break
                
                if "(" in raw and "円" in raw:
                    cleaned = raw.replace("(", "").replace(")", "").replace("円", "").replace(",", "").strip()
                    if cleaned and cleaned.replace(".", "").replace("-", "").isdigit():
                        manage_value = float(cleaned)
                        break

                if "円" in raw and "万" not in raw and "(" not in raw:
                    cleaned = raw.replace("円", "").replace(",", "").strip()
                    if cleaned and cleaned.replace(".", "").replace("-", "").isdigit():
                        manage_value = float(cleaned)
                        break

        except Exception as e:
            manage_value = 0

    # 合算
    rent_value = rent_value + manage_value

    # パターンB
    if rent_value == 0:
        try:
            rent_elem = WebDriverWait(driver, 3).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, ".property_view_main-emphasis")
                )
            )

            rent_text = rent_elem.text

            if "万円" in rent_text:
                rent_value = float(rent_text.replace("万円","").strip()) * 10000
            else:
                rent_value = float(
                    rent_text.replace("円","").replace(",","").strip()
                )

        except:
            rent_value = 0
    
    # パターンC: property_view_note-emphasis
    if rent_value == 0:
        try:
            rent_elem = driver.find_element(
                By.CSS_SELECTOR,
                ".property_view_note-emphasis"
            )
            rent_text = rent_elem.text

            if "万円" in rent_text:
                rent_value = float(rent_text.replace("万円","").strip()) * 10000
            elif "円" in rent_text:
                rent_value = float(rent_text.replace("円","").replace(",","").strip())
            
            # パターンCで取得した場合、管理費も再チェック
            if manage_value == 0:
                try:
                    all_spans = driver.find_elements(By.XPATH, "//div[@class='property_view_note-list']//span")
                    for span in all_spans:
                        text = span.text
                        if ("管理費" in text or "共益費" in text) and "円" in text:
                            match = re.search(r'(\d+(?:,\d+)*)\s*円', text)
                            if match:
                                manage_value = float(match.group(1).replace(",", ""))
                                rent_value = rent_value + manage_value
                                break
                except Exception as e:
                    pass

        except:
            rent_value = 0

    if area_value > 0 and rent_value > 0:
        unit_price = rent_value / area_value
        price_5008 = unit_price * 50.08
        price_5446 = unit_price * 54.46
    else:
        unit_price = 0
        price_5008 = 0
        price_5446 = 0

    results.append([
        name,
        station,
        age,
        area_value,
        rent_value,
        round(unit_price),
        round(price_5008),
        round(price_5446)
    ])

driver.quit()

# データフレーム作成
df = pd.DataFrame(results, columns=[
    "物件名",
    "駅・徒歩",
    "築年数",
    "面積",
    "賃料",
    "平米単価",
    "50.08m2想定",
    "54.46m2想定"
])

# 今日の日付と平均値を計算
today = datetime.now().strftime("%Y-%m-%d")
avg_5008 = df["50.08m2想定"].mean()
avg_5446 = df["54.46m2想定"].mean()

print(f"\n本日の平均値:")
print(f"50.08m2想定: {avg_5008:,.0f}円")
print(f"54.46m2想定: {avg_5446:,.0f}円")

# 履歴ファイルに追加
if os.path.exists(HISTORY_FILE):
    # 既存の履歴を読み込み
    history_df = pd.read_csv(HISTORY_FILE, encoding="utf-8-sig")
else:
    # 新規作成
    history_df = pd.DataFrame(columns=["日付", "50.08m2平均", "54.46m2平均", "物件数"])

# 今日のデータを追加
new_row = pd.DataFrame([{
    "日付": today,
    "50.08m2平均": round(avg_5008),
    "54.46m2平均": round(avg_5446),
    "物件数": len(links)
}])

history_df = pd.concat([history_df, new_row], ignore_index=True)

# 重複削除（同じ日付の場合は最新のデータのみ保持）
history_df = history_df.drop_duplicates(subset=["日付"], keep="last")

# 日付順にソート
history_df = history_df.sort_values("日付")

# 履歴CSVを保存
history_df.to_csv(HISTORY_FILE, index=False, encoding="utf-8-sig")
print(f"\n履歴を保存: {HISTORY_FILE}")

# グラフ作成（2回以上のデータがある場合）
if len(history_df) >= 2:
    plt.figure(figsize=(12, 6))
    
    # 日付を変換
    dates = pd.to_datetime(history_df["日付"])
    
    # 2つの系列をプロット
    plt.plot(dates, history_df["50.08m2平均"], marker='o', label='50.08m² 平均', linewidth=2)
    plt.plot(dates, history_df["54.46m2平均"], marker='s', label='54.46m² 平均', linewidth=2)
    
    # グラフの装飾
    plt.xlabel('日付', fontsize=12)
    plt.ylabel('想定賃料（円）', fontsize=12)
    plt.title('賃料推移（週次）', fontsize=14, fontweight='bold')
    plt.legend(fontsize=11)
    plt.grid(True, alpha=0.3)
    
    # 日付フォーマット
    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
    plt.gcf().autofmt_xdate()
    
    # Y軸のフォーマット（カンマ区切り）
    ax = plt.gca()
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f'{int(x):,}'))
    
    # グラフを保存
    plt.tight_layout()
    plt.savefig('/home/taki/suumo_trend.png', dpi=150)
    print(f"グラフを保存: /home/taki/suumo_trend.png")
    plt.close()

# 詳細データCSVも保存
search_conditions = []
search_conditions.append(["検索日時", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
search_conditions.append(["検索URL", SEARCH_URL])
search_conditions.append(["取得件数", len(links)])
search_conditions.append([])

conditions_df = pd.DataFrame(search_conditions)

csv_filename = f"/home/taki/suumo_list_{today}.csv"
with open(csv_filename, "w", encoding="utf-8-sig") as f:
    conditions_df.to_csv(f, index=False, header=False)
    df.to_csv(f, index=False)

print(f"詳細データを保存: {csv_filename}")
print("\nCSV 出力完了")
