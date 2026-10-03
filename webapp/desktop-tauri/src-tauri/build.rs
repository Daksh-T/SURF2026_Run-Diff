fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(&["save_export"])),
    )
    .expect("could not build Run·Diff permissions")
}
