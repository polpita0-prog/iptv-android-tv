name: Budowanie APK na Android TV

on:
  push:
    branches: [ "main", "master" ]
  workflow_dispatch:

jobs:
  build-apk:
    runs-on: ubuntu-latest

    steps:
      - name: Pobieranie kodu z repozytorium
        uses: actions/checkout@v4

      - name: Instalacja wymaganych bibliotek systemowych Linux (GTK 3)
        run: |
          sudo apt-get update
          sudo apt-get install -y libgtk-3-dev clang cmake ninja-build pkg-config

      - name: Konfiguracja środowiska Java 17
        uses: actions/setup-java@v4
        with:
          distribution: 'temurin'
          java-version: '17'

      - name: Konfiguracja Pythona
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Konfiguracja Fluttera
        uses: subosito/flutter-action@v2
        with:
          channel: 'stable'
          flutter-version: '3.24.3'

      - name: Akceptacja wszystkich licencji Android SDK
        run: |
          yes | sudo $ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager --licenses || true
          yes | flutter doctor --android-licenses || true

      - name: Instalacja Flet i budowanie pliku APK
        run: |
          pip install "flet[all]==0.24.1" flet-video requests
          flet build apk --verbose --no-rich-output

      - name: Udostępnienie gotowego pliku APK do pobrania
        uses: actions/upload-artifact@v4
        with:
          name: IPTV-Client-PRO-AndroidTV-APK
          path: build/apk/*.apk
