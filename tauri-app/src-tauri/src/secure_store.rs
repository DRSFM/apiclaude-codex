use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};

const CLEARED_MARKER: &[u8] = b"APIAGENT-CLEARED-V1\0";

pub fn credential_store_root() -> PathBuf {
    dirs::home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".apiagent-secrets")
}

fn credential_path(root: &Path, credential_id: &str) -> PathBuf {
    let digest = Sha256::digest(credential_id.as_bytes());
    root.join(format!("{:x}.bin", digest))
}

fn entropy(credential_id: &str) -> Vec<u8> {
    format!("apiagent:v1:{}", credential_id).into_bytes()
}

pub fn set_secret(credential_id: &str, secret: &str) -> Result<(), String> {
    set_secret_at(&credential_store_root(), credential_id, secret)
}

pub fn get_secret(credential_id: &str) -> Result<String, String> {
    get_secret_at(&credential_store_root(), credential_id)
}

pub fn clear_secret(credential_id: &str) -> Result<(), String> {
    let path = credential_path(&credential_store_root(), credential_id);
    if path.exists() {
        let size = fs::metadata(&path)
            .map(|metadata| metadata.len() as usize)
            .unwrap_or(64)
            .max(64);
        let mut cleared = Vec::with_capacity(size);
        cleared.extend_from_slice(CLEARED_MARKER);
        cleared.resize(size, 0);
        fs::write(path, cleared)
            .map_err(|error| format!("Failed to clear secure credential: {}", error))?;
    }
    Ok(())
}

fn set_secret_at(root: &Path, credential_id: &str, secret: &str) -> Result<(), String> {
    if credential_id.is_empty() {
        return Err("credential_id cannot be empty".to_string());
    }
    if secret.is_empty() {
        return Err("secret cannot be empty".to_string());
    }

    let encrypted = protect(secret.as_bytes(), &entropy(credential_id))?;
    fs::create_dir_all(root)
        .map_err(|error| format!("Failed to create secure credential directory: {}", error))?;
    fs::write(credential_path(root, credential_id), encrypted)
        .map_err(|error| format!("Failed to write secure credential: {}", error))?;
    if get_secret_at(root, credential_id)? != secret {
        return Err(format!(
            "Failed to verify stored credential '{}'",
            credential_id
        ));
    }
    Ok(())
}

fn get_secret_at(root: &Path, credential_id: &str) -> Result<String, String> {
    let path = credential_path(root, credential_id);
    let encrypted =
        fs::read(path).map_err(|_| format!("Credential '{}' is not stored", credential_id))?;
    if encrypted.starts_with(CLEARED_MARKER) {
        return Err(format!("Credential '{}' is not stored", credential_id));
    }
    let plaintext = unprotect(&encrypted, &entropy(credential_id))?;
    String::from_utf8(plaintext)
        .map_err(|_| format!("Credential '{}' contains invalid data", credential_id))
}

#[cfg(windows)]
fn protect(data: &[u8], entropy: &[u8]) -> Result<Vec<u8>, String> {
    use std::ptr;
    use windows_sys::Win32::Foundation::LocalFree;
    use windows_sys::Win32::Security::Cryptography::{
        CryptProtectData, CRYPTPROTECT_UI_FORBIDDEN, CRYPT_INTEGER_BLOB,
    };

    let input = CRYPT_INTEGER_BLOB {
        cbData: data.len() as u32,
        pbData: data.as_ptr() as *mut u8,
    };
    let optional_entropy = CRYPT_INTEGER_BLOB {
        cbData: entropy.len() as u32,
        pbData: entropy.as_ptr() as *mut u8,
    };
    let description: Vec<u16> = "API Agent credential\0".encode_utf16().collect();
    let mut output = CRYPT_INTEGER_BLOB {
        cbData: 0,
        pbData: ptr::null_mut(),
    };

    let success = unsafe {
        CryptProtectData(
            &input,
            description.as_ptr(),
            &optional_entropy,
            ptr::null(),
            ptr::null(),
            CRYPTPROTECT_UI_FORBIDDEN,
            &mut output,
        )
    };
    if success == 0 {
        return Err(format!(
            "Windows DPAPI encryption failed with error {}",
            std::io::Error::last_os_error()
        ));
    }

    let encrypted =
        unsafe { std::slice::from_raw_parts(output.pbData, output.cbData as usize).to_vec() };
    unsafe {
        LocalFree(output.pbData.cast());
    }
    Ok(encrypted)
}

#[cfg(windows)]
fn unprotect(data: &[u8], entropy: &[u8]) -> Result<Vec<u8>, String> {
    use std::ptr;
    use windows_sys::Win32::Foundation::LocalFree;
    use windows_sys::Win32::Security::Cryptography::{
        CryptUnprotectData, CRYPTPROTECT_UI_FORBIDDEN, CRYPT_INTEGER_BLOB,
    };

    let input = CRYPT_INTEGER_BLOB {
        cbData: data.len() as u32,
        pbData: data.as_ptr() as *mut u8,
    };
    let optional_entropy = CRYPT_INTEGER_BLOB {
        cbData: entropy.len() as u32,
        pbData: entropy.as_ptr() as *mut u8,
    };
    let mut output = CRYPT_INTEGER_BLOB {
        cbData: 0,
        pbData: ptr::null_mut(),
    };

    let success = unsafe {
        CryptUnprotectData(
            &input,
            ptr::null_mut(),
            &optional_entropy,
            ptr::null(),
            ptr::null(),
            CRYPTPROTECT_UI_FORBIDDEN,
            &mut output,
        )
    };
    if success == 0 {
        return Err(format!(
            "Windows DPAPI decryption failed with error {}",
            std::io::Error::last_os_error()
        ));
    }

    let plaintext =
        unsafe { std::slice::from_raw_parts(output.pbData, output.cbData as usize).to_vec() };
    unsafe {
        LocalFree(output.pbData.cast());
    }
    Ok(plaintext)
}

#[cfg(not(windows))]
fn protect(_data: &[u8], _entropy: &[u8]) -> Result<Vec<u8>, String> {
    Err("Secure credential storage currently requires Windows DPAPI".to_string())
}

#[cfg(not(windows))]
fn unprotect(_data: &[u8], _entropy: &[u8]) -> Result<Vec<u8>, String> {
    Err("Secure credential storage currently requires Windows DPAPI".to_string())
}

#[cfg(test)]
mod tests {
    use super::{get_secret_at, set_secret_at};
    use std::fs;

    #[cfg(windows)]
    #[test]
    fn dpapi_round_trip_does_not_store_plaintext() {
        let root =
            std::env::temp_dir().join(format!("apiagent-secure-store-test-{}", std::process::id()));
        fs::create_dir_all(&root).unwrap();
        let credential_id = "claude:rust-test";
        let secret = "sk-test-rust-secret";

        set_secret_at(&root, credential_id, secret).unwrap();

        let stored = fs::read_dir(&root).unwrap().next().unwrap().unwrap().path();
        let bytes = fs::read(stored).unwrap();
        assert!(!bytes
            .windows(secret.len())
            .any(|window| window == secret.as_bytes()));
        assert_eq!(get_secret_at(&root, credential_id).unwrap(), secret);

        fs::remove_dir_all(root).unwrap();
    }
}
