[app]
title = SpamGuard ES
package.name = spamguardes
package.domain = org.spamguard
source.dir = .
source.include_exts = py,json,xml,txt,png,jpg,atlas
source.exclude_dirs = tests,bin,.git,__pycache__
version = 0.1.0
requirements = python3,kivy,pyjnius
orientation = portrait
fullscreen = 0
android.permissions = INTERNET,RECEIVE_BOOT_COMPLETED
android.api = 36
android.minapi = 29
android.ndk = 29
android.archs = arm64-v8a,armeabi-v7a
android.accept_sdk_license = True
android.add_src = android_src
p4a.hook = p4a_hook.py
p4a.branch = develop

[buildozer]
log_level = 2
warn_on_root = 1
