# KRX 외국인·기관 수급 대시보드

기존 `DS_GHJ_VERSION_04.ipynb`의 Selenium 자동화를 KRX 정보데이터시스템의 내부 JSON 요청 방식으로 바꾼 Flask 웹 애플리케이션입니다.

## 제공 기능

- KRX 계정으로 로그인하여 외국인·기관합계 순매수 데이터 조회
- 기준일 기준 1개월·3개월·6개월 누적 조회
- 최근 실제 거래일 5일 자동 탐색
- `base`, `last` 시트를 포함한 통합 Excel 생성
- 외국인 TOP10, 외국인·기관 비교, 누적·최근 추이, TOP20 히트맵 표시
- KOSPI·KOSDAQ·KONEX·전체시장 선택

## 로컬 실행

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

브라우저에서 `http://127.0.0.1:5000`으로 접속합니다.

## Vercel

Vercel은 루트의 `app.py`에 노출된 Flask `app`을 자동으로 감지합니다. GitHub 저장소를 Vercel 프로젝트로 가져오면 `main` 브랜치 푸시마다 자동 배포됩니다. 함수 실행시간은 Vercel 프로젝트의 Functions 설정에서 사용하는 요금제 한도 내 최댓값으로 설정하는 것을 권장합니다.

필수 환경변수:

- `FLASK_SECRET_KEY`: 충분히 긴 임의 문자열

KRX 아이디와 비밀번호는 웹 폼의 해당 요청에서만 사용되며 코드, Excel, 로그에 저장하지 않습니다.
CSRF 검증, 요청 크기 제한, 보안 응답 헤더와 안전한 세션 쿠키 설정을 적용합니다.

## 서버리스 저장 제한

Vercel에서는 생성 파일을 `/tmp`에 임시 저장합니다. 다운로드 링크는 30분 동안 유효하지만 서버 인스턴스가 바뀌면 더 일찍 만료될 수 있습니다. 영구 보관과 실행 이력을 위해서는 다음 단계에서 Supabase Storage와 PostgreSQL을 연결합니다.

## 보안

- `.env`, Excel 결과물, 실행 출력 폴더는 Git에서 제외됩니다.
- 계정 비밀번호나 API 키를 소스 코드 및 GitHub에 커밋하지 마세요.
- 원본 노트북에 들어 있던 Gmail 앱 비밀번호는 폐기하고 새로 발급해야 합니다.
