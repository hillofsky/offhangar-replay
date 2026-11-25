# OffHangar Replay

**한국어** · [English](README.en.md)

월드 오브 탱크 **EU 2.3.1.2 (Windows x64)** 및 **OffHangar 2.0 LAN**용 오프라인 전투 리플레이 모드입니다. 전투 종료 후 게임의 기본 재생기로 열 수 있는 `.wotreplay`를 만듭니다. 재생할 PC에도 같은 클라이언트와 이 모드를 설치해야 전투 기록, 무전, 소모품 등 추가 HUD 기능을 볼 수 있습니다.

## 설치

1. [최신 릴리스](https://github.com/hillofsky/offhangar-replay/releases/latest)에서 `offhangar-replay-2.3.1.2-win64.zip`을 받아 별도 폴더에 압축을 풉니다.
2. 게임을 닫습니다. 배포 ZIP에 EU 2.3.1.2용 리플레이 초기 데이터가 들어 있습니다.
3. 압축을 푼 폴더에서 PowerShell을 열고 아래 명령을 실행합니다. 게임 경로를 본인의 설치 폴더로 바꿉니다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Install.ps1 -GameDir "D:\Games\World_of_Tanks"
```

신규 설치에서는 동봉한 `bootstrap.dat`를 사용하고, 업데이트에서는 기존 템플릿을 재사용합니다. 별도 템플릿을 쓰려면 설치 명령에 `-Replay "D:\Replays\my-native-battle.wotreplay"`를 추가하세요. 이 옵션은 호환되는 같은 버전의 기본 게임 리플레이를 받습니다.

## 녹화와 재생

게임 설정에서 녹화 방식을 선택한 다음 오프라인 전투를 시작합니다. 전투가 끝나면 게임 폴더의 `replays`에서 완성된 파일을 열 수 있습니다.

| 게임 설정 | 저장 결과 |
| --- | --- |
| 끄기 | 새 전투를 녹화하지 않습니다. |
| 최근 | `OffHangar-last.wotreplay`를 마지막 전투로 갱신합니다. |
| 전체 | 전투마다 날짜·시간이 붙은 `OffHangar-*.wotreplay`를 저장합니다. |

기본값은 **플레이어에게 표시된 전차만 녹화**입니다. 차고에서 `Ctrl + Alt + F9`를 누르거나 `mods\offhangar-fleet\FleetReplaySettings.exe`에서 전체 전차 녹화로 바꿀 수 있습니다. 다음 전투부터 적용됩니다. 이 선택은 전차의 기록 범위를 정합니다. 파일 보관 개수는 위의 게임 설정에서 선택합니다.

### 기록되는 전투 정보

- 전차 이동과 방향, 포탑·포신 회전, 발사·피격·격파, 플레이어 카메라와 저격 시점
- 조준원·장전 상태, 육감, 무전 명령, 조준한 전차의 윤곽선
- 탄종별 잔탄과 탄창, 현재·다음 탄종, 소모품 수량과 재사용 대기시간
- 왼쪽 하단 전투 기록과 누적 합계. 가한 피해, 도움 피해, 막은 피해, 받은 피해와 부품·승무원 피해를 원래 HUD로 표시합니다.

## 호환성과 문제 확인

- 지원 대상으로 확인한 기반 모드는 `mods\2.3.1.2\offhangar2_lan_2312.wotmod`입니다. 설치하려는 PC에 해당 모드와 게임이 먼저 있어야 합니다.
- 기존 리플레이에서는 체력 변화와 공격자 기록이 있는 피해를 복원할 수 있습니다. 당시 저장되지 않은 탄약, 소모품, 도움 피해를 정확하게 표시하려면 새로 녹화해야 합니다.
- 아군만 담긴 짧은 프로호로프카 테스트 파일 한 개에서 로딩 중 클라이언트가 종료된 사례가 있습니다. 원인은 확인되지 않았습니다.
- `.ohtrack`은 변환 전 원본입니다. 전투 종료 후 `replays\offhangar`에 `.export-error.txt`가 생기면 해당 파일에서 변환 실패 원인을 확인할 수 있습니다.
- 리플레이 또는 `bootstrap.dat`에는 전차장 이름과 전투 식별 정보가 포함될 수 있습니다. 오류 제보에는 우선 게임 버전, 지도, 녹화 모드, 문제가 발생한 재생 시각을 적어 주세요.

## 소스에서 빌드

Windows x64, 64비트 Python 3.12, 위 게임 설치본, .NET Framework 4.x C# 컴파일러가 필요합니다. 게임 내 모듈은 해당 클라이언트의 `win64\python27.dll`로 컴파일합니다.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe .\tools\build.py "D:\Games\World_of_Tanks"
```

`build`에 Windows 배포 ZIP과 `SHA256SUMS.txt`가 생성됩니다. 배포 ZIP에는 모드 6개, 변환기, 녹화 범위 설정 도구, 설치 스크립트와 번들 라이브러리 고지가 포함됩니다.

| 경로 | 내용 |
| --- | --- |
| `src/mod_oh2_*.py` | 게임 안에서 실행되는 녹화·재생 모듈 |
| `src/fleet_export.py`, `src/replay_sample.py` | 전투 기록 변환과 네이티브 리플레이 컨테이너 처리 |
| `src/FleetReplaySettings.cs` | 녹화 범위 설정 도구 |
| `tools/` | Windows 배포본 빌드 |
| `templates/bootstrap.dat` | 기본 설치에 사용하는 재생 초기 데이터 |
| `Install.ps1` | 호환 템플릿 검사와 설치 |

## 제거

게임을 닫고 `mods\2.3.1.2`에서 `oh2-fleet.wotmod`, `oh2-fleet-animation.wotmod`, `oh2-fleet-hud.wotmod`, `oh2-fleet-radio.wotmod`, `oh2-fleet-status.wotmod`, `oh2-replay-compat.wotmod`를 제거합니다. 변환기와 설정은 `mods\offhangar-fleet`에 있습니다. 저장한 전투는 `replays`에 있습니다.
