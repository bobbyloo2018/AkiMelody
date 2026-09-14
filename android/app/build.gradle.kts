import org.gradle.api.tasks.Sync

plugins {
    id("com.android.application")
    id("com.chaquo.python")
}

val repositoryRoot = rootProject.projectDir.parentFile
val generatedPython = layout.buildDirectory.dir("generated/akiPython/main")

val mobileNewRoot = repositoryRoot.resolve("Mobile_New")

val syncAkiPython by tasks.registering(Sync::class) {
    // Backend shared with desktop
    from(repositoryRoot) {
        include(
            "app.py",
            "artwork_fetcher.py",
            "data_paths.py",
            "lockin_taxonomy.py",
            "security_utils.py",
            "ytmusic_auth.py",
            "CHANGELOG.md",
            "static/fontawesome/**",
            "static/js/modules/spotify-importer.js",
            // classic kept as fallback, not removed
            "templates/mobile.html",
            "static/css/mobile.css",
            "static/js/mobile.js",
        )
    }
    // OTHER mobile frontend (mobile_player) — now the primary, sourced from
    // Mobile_New so edits in the new directory are what gets packaged.
    // This is the elaborate Aki shell (mobile_player.html) you requested.
    from(mobileNewRoot) {
        include(
            "templates/mobile_player.html",
            "static/css/mobile_player.css",
            "static/js/mobile_player.js",
        )
    }
    into(generatedPython)
}

android {
    namespace = "com.akimelody.app"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.akimelody.app"
        minSdk = 24
        targetSdk = 35
        versionCode = 1
        versionName = "1.0.0-alpha01"

        ndk {
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    buildTypes {
        debug {
            isMinifyEnabled = false
        }
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        buildConfig = true
    }

    sourceSets.getByName("main") {
        java.srcDir("src/main/java")
    }
}

chaquopy {
    defaultConfig {
        version = "3.12"
        providers.environmentVariable("AKI_ANDROID_PYTHON").orNull?.let { buildPython(it) }
        pip {
            install("-r", "requirements-android.txt")
        }
        pyc {
            src = true
            pip = true
        }
    }
    sourceSets.getByName("main") {
        srcDir(generatedPython)
    }
}

tasks.named("preBuild").configure {
    dependsOn(syncAkiPython)
}

// Chaquopy reads the generated source directory in its variant-specific merge
// tasks. Gradle 8.13 requires that producer/consumer relationship to be
// explicit instead of relying on preBuild ordering alone.
tasks.matching {
    it.name.startsWith("merge") && it.name.endsWith("PythonSources")
}.configureEach {
    dependsOn(syncAkiPython)
}
