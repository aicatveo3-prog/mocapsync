import Combine
import Foundation
import SwiftUI

// ─────────────────────────────────────────────────────────────────────────────
// 측정한 클럭 오프셋을 앱 전체에서 공유합니다.
//
// ★ 이 파일이 왜 생겼나 — 3단계 첫 실기기 시험의 실패
//
// 녹화는 됐는데 사이드카 자체검증이 "사용 불가"로 나왔습니다.
// 원인: 동기 화면(SyncView)과 녹화 화면(RecordView)이 서로를 몰랐습니다.
// 각자 자기 객체를 들고 있어서, 동기를 아무리 잘 해도 녹화 쪽 오프셋은
// 계속 0 이었고 검증이 `no_clock_sync` 치명으로 잡았습니다.
//
// 검증이 제 역할을 한 셈입니다. 이 장치가 없었으면 오프셋 0 인 영상을
// PC 까지 올려보내고 나서야 알았을 겁니다.
//
// ★ 단순 전달로 끝나지 않습니다
//
// 오프셋은 재부팅·절전·드리프트로 낡습니다. 그래서 값만 넘기지 않고
// **언제·어떤 상태에서 측정했는지**를 함께 들고 다니며, 쓸 때마다
// 아직 유효한지 판정합니다. 판정 로직은 MocapSyncCore.ClockOffsetRecord 에
// 있습니다 (시뮬레이터 없이 CI 에서 시험됩니다).
// ─────────────────────────────────────────────────────────────────────────────

@MainActor
final class SyncStore: ObservableObject {

    static let shared = SyncStore()

    /// 가장 최근 측정
    @Published private(set) var latest: ClockOffsetRecord?
    /// 직전 측정. 두 개가 있으면 드리프트를 실측할 수 있습니다.
    @Published private(set) var previous: ClockOffsetRecord?

    /// 실측된 상대 드리프트 (ppm). 측정 불가하면 nil.
    @Published private(set) var measuredDriftPpm: Double?
    /// 그 측정의 신뢰 구간 (ppm). 실측값이 이보다 작으면 "측정 못 함"입니다.
    @Published private(set) var driftUncertaintyPpm: Double?

    private let latestKey = "mocapsync.sync.latest"
    private let previousKey = "mocapsync.sync.previous"

    private init() { load() }

    // MARK: - 저장

    /// 동기 측정이 끝나면 호출합니다.
    func record(_ est: SyncEstimate, masterId: String) {
        let r = ClockOffsetRecord(
            offsetNs: est.offsetNs,
            uncertaintyNs: est.uncertaintyNs,
            minRttNs: est.minRttNs,
            measuredAtSlaveNs: MonotonicClock.nowNs(),
            sleepAtSyncNs: MonotonicClock.cumulativeSleepNs(),
            bootTimeEpochNs: MonotonicClock.bootTimeEpochNs(),
            masterId: masterId)

        previous = latest
        latest = r
        recomputeDrift()
        save()

        AppLog.shared.i("SyncStore", String(
            format: "오프셋 저장: %+.3f ms (상한 %.3f ms), 측정시각 %.1f초, 절전누적 %.1f초",
            r.offsetNs.ms, r.uncertaintyNs.ms,
            Double(r.measuredAtSlaveNs) / 1e9, Double(r.sleepAtSyncNs) / 1e9))

        if let ppm = measuredDriftPpm, let unc = driftUncertaintyPpm {
            if abs(ppm) > unc {
                AppLog.shared.i("SyncStore", String(
                    format: "★ 드리프트 실측: %+.2f ppm (신뢰구간 ±%.2f ppm). "
                          + "규격 최악값 40 ppm 과 비교하세요.", ppm, unc))
            } else {
                AppLog.shared.i("SyncStore", String(
                    format: "드리프트 미측정: 실측 %+.2f ppm 이 신뢰구간 ±%.2f ppm 안입니다. "
                          + "두 측정 간격을 더 벌려야 구분됩니다.", ppm, unc))
            }
        }
    }

    private func recomputeDrift() {
        guard let a = previous, let b = latest else {
            measuredDriftPpm = nil
            driftUncertaintyPpm = nil
            return
        }
        measuredDriftPpm = ClockOffsetRecord.driftPpm(from: a, to: b)
        driftUncertaintyPpm = ClockOffsetRecord.driftUncertaintyPpm(from: a, to: b)
    }

    // MARK: - 사용

    /// 지금 이 오프셋이 유효한지.
    func freshness() -> ClockOffsetRecord.Freshness {
        guard let r = latest else { return .invalid }
        return r.freshness(nowSlaveNs: MonotonicClock.nowNs(),
                           nowSleepNs: MonotonicClock.cumulativeSleepNs(),
                           nowBootTimeEpochNs: MonotonicClock.bootTimeEpochNs(),
                           driftPpm: effectiveDriftPpm)
    }

    /// 드리프트 예산에 쓸 ppm.
    ///
    /// 실측값이 신뢰 구간을 넘었다면 그걸 씁니다(단, 최소 5 ppm 은 남겨 둡니다 —
    /// 실측이 우연히 0 에 가까웠을 수 있으므로 0 으로 놓는 것은 위험합니다).
    /// 실측이 안 됐으면 규격 최악값을 씁니다.
    var effectiveDriftPpm: Double {
        guard let ppm = measuredDriftPpm, let unc = driftUncertaintyPpm,
              abs(ppm) > unc else {
            return ClockOffsetRecord.worstCaseDriftPpm
        }
        return max(abs(ppm) + unc, 5)
    }

    /// 녹화기에 넘길 스냅샷.
    ///
    /// ★ 측정한 상한을 **그대로** 넣습니다. 드리프트를 미리 더하지 않습니다.
    ///
    /// 처음에는 드리프트를 더한 '실효 상한'을 넣으려 했습니다. 시간이 지난 만큼
    /// 어긋났고 그건 측정 상한에 포함되지 않으니까요. 그런데 두 가지 문제가 있습니다.
    ///
    ///  (1) 사이드카 설계 원칙 위반. "변환하지 않고 원본을 저장한다"가 원칙입니다.
    ///      합쳐서 저장하면 나중에 드리프트 가정이 틀렸다는 걸 알아도 되돌릴 수 없습니다.
    ///  (2) 판정 문구가 거짓말을 합니다. 상한이 3 ms 를 넘으면 사이드카 검증이
    ///      "WiFi 상태가 평소보다 안 좋습니다"라고 말하는데, 실제 원인은 드리프트입니다.
    ///
    /// 사이드카에는 `clockMeasuredAtNs` 와 프레임 시각이 모두 들어 있으므로
    /// **나이는 데이터에서 계산됩니다.** 드리프트 판정은 검증 쪽에서 합니다.
    /// 화면에는 `effectiveUncertaintyNs` 로 계산한 값을 따로 보여줍니다.
    func snapshotForRecording() -> Recorder.SyncSnapshot {
        guard let r = latest else { return .empty }
        return Recorder.SyncSnapshot(
            offsetNs: r.offsetNs,
            uncertaintyNs: r.uncertaintyNs,
            minRttNs: r.minRttNs,
            measuredAtSlaveNs: r.measuredAtSlaveNs,
            sleepAtSyncNs: r.sleepAtSyncNs)
    }

    /// 화면 표시용: 지금 시점의 드리프트 포함 상한.
    var displayEffectiveUncertaintyNs: Int64 {
        guard let r = latest else { return 0 }
        let age = MonotonicClock.nowNs() - r.measuredAtSlaveNs
        return r.effectiveUncertaintyNs(ageNs: age, driftPpm: effectiveDriftPpm)
    }

    // MARK: - 영속

    /// ★ 저장하는 이유와 위험
    ///
    /// 앱을 다시 켤 때마다 동기를 강제하면 불편합니다. 그래서 저장합니다.
    /// 다만 저장된 값은 대개 무효입니다 — 앱을 닫아 둔 동안 폰이 잤을 테니까요.
    /// 그래서 `freshness()` 가 절전·재부팅을 잡아내 자동으로 무효 처리합니다.
    /// **저장 자체가 위험한 게 아니라, 검증 없이 쓰는 것이 위험합니다.**
    private func save() {
        let enc = JSONEncoder()
        if let r = latest, let d = try? enc.encode(r) {
            UserDefaults.standard.set(d, forKey: latestKey)
        }
        if let p = previous, let d = try? enc.encode(p) {
            UserDefaults.standard.set(d, forKey: previousKey)
        }
    }

    private func load() {
        let dec = JSONDecoder()
        if let d = UserDefaults.standard.data(forKey: latestKey) {
            latest = try? dec.decode(ClockOffsetRecord.self, from: d)
        }
        if let d = UserDefaults.standard.data(forKey: previousKey) {
            previous = try? dec.decode(ClockOffsetRecord.self, from: d)
        }
        recomputeDrift()
        if latest != nil {
            AppLog.shared.i("SyncStore", "저장된 오프셋 복원: \(freshness().summary)")
        }
    }

    func clear() {
        latest = nil
        previous = nil
        measuredDriftPpm = nil
        driftUncertaintyPpm = nil
        UserDefaults.standard.removeObject(forKey: latestKey)
        UserDefaults.standard.removeObject(forKey: previousKey)
        AppLog.shared.i("SyncStore", "저장된 오프셋 삭제")
    }
}
