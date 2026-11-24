# market-signals

GitHub Actions가 평일 하루 두 번(미국 마감 후 22:40 UTC, 한국 마감 후 07:40 UTC) 지표를 받아 `data/latest.json`에 씁니다. 매일 장세 판정 작업이 이 파일을 먼저 읽습니다.

## 설정 (한 번만)
1. https://fred.stlouisfed.org/docs/api/api_key.html 에서 무료 API 키 발급
2. 저장소 **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `FRED_API_KEY`, Value: 발급받은 키
3. **Actions** 탭 → `fetch-market-data` → **Run workflow**로 첫 실행 확인

## 수집 항목
- FRED: DGS10, DGS2, DFEDTARU, BAMLH0A0HYM2, DEXJPUS, DEXKOUS, UNRATE, SAHMREALTIME, IC4WSA, NFCI, VIXCLS, SP500
- Yahoo(근사치): S&P500·코스피 종가와 50/200일선, VIX, USD/KRW, USD/JPY, S&P500 200일선 위 종목 비율
- KRX: 외국인 코스피 순매매(억원, pykrx → 실패 시 네이버 금융), 연속 순매수·순매도 일수
- 파생값: 10년물 5% 위 연속일수, 2년물−기준금리 상단, 하이일드 6개월 최저·최고 대비, 엔 1개월 절상률, VIX 25/30 연속일수 등

실패한 항목은 `errors`에 남고 나머지는 계속 수집됩니다.
