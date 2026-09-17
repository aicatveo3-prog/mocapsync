// 루트 빌드 스크립트. 플러그인을 "선언만" 하고 적용은 :app 모듈에서 합니다.
plugins {
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.kotlin.android) apply false
    alias(libs.plugins.kotlin.compose) apply false
}
