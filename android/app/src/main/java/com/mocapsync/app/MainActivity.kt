package com.mocapsync.app

import android.os.Build
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import com.mocapsync.app.core.AppLog
import com.mocapsync.app.ui.MocapApp
import com.mocapsync.app.ui.MocapTheme

class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // 촬영/동기 테스트 중에 화면이 꺼지면 곤란합니다.
        // (2단계부터는 Foreground Service + wake lock 으로 제대로 처리합니다)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        AppLog.i(
            "App",
            "앱 시작 v${BuildConfig.VERSION_NAME} (code ${BuildConfig.VERSION_CODE}) " +
                "sha=${BuildConfig.GIT_SHA} 빌드=${BuildConfig.BUILD_STAMP} ciRun=${BuildConfig.CI_RUN}"
        )
        AppLog.i(
            "App",
            "기기 ${Build.MANUFACTURER} ${Build.MODEL} / Android ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT})"
        )

        setContent {
            MocapTheme {
                MocapApp()
            }
        }
    }

    override fun onDestroy() {
        AppLog.i("App", "앱 종료")
        super.onDestroy()
    }
}
