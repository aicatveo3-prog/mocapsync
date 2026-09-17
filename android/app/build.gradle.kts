import java.time.ZoneOffset
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
}

// ─────────────────────────────────────────────────────────────────────────────
// 빌드 식별 정보.
// 실기기 테스트를 사람이 하기 때문에 "내가 지금 어떤 커밋의 APK를 깔았는지"를
// 앱 화면과 로그에서 즉시 확인할 수 있어야 합니다. CI 환경변수를 그대로 심습니다.
// ─────────────────────────────────────────────────────────────────────────────
val ciRunNumber: Int = (System.getenv("GITHUB_RUN_NUMBER") ?: "0").toIntOrNull() ?: 0
val gitSha: String = (System.getenv("GITHUB_SHA") ?: "local").take(7)
val buildStamp: String = DateTimeFormatter
    .ofPattern("yyyy-MM-dd HH:mm:ss 'UTC'")
    .format(ZonedDateTime.now(ZoneOffset.UTC))

android {
    namespace = "com.mocapsync.app"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.mocapsync.app"
        minSdk = 26        // Android 8.0. 삼성 최근 기기는 전부 충족.
        targetSdk = 35
        // CI 빌드마다 versionCode 가 올라가야 폰에서 "업데이트 설치"가 깔끔합니다.
        versionCode = ciRunNumber + 1
        versionName = "0.1.$ciRunNumber-step1"

        buildConfigField("String", "GIT_SHA", "\"$gitSha\"")
        buildConfigField("String", "BUILD_STAMP", "\"$buildStamp\"")
        buildConfigField("int", "CI_RUN", "$ciRunNumber")
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    buildTypes {
        debug {
            isMinifyEnabled = false
        }
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    // APK 파일 이름 변경은 Gradle 쪽에서 하지 않습니다.
    // (applicationVariants.all 은 AGP 에서 deprecated 이고 Gradle 9 와 충돌합니다)
    // CI 워크플로에서 app-debug.apk -> mocapsync-debug.apk 로 rename 합니다.

    lint {
        // 1단계에서 lint 경고 때문에 CI가 멈추지 않게 합니다
        abortOnError = false
        checkReleaseBuilds = false
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.activity.compose)

    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.graphics)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.compose.material3)

    debugImplementation(libs.androidx.compose.ui.tooling)
}
