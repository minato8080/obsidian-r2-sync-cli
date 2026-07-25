import { Cipher } from "@fyears/rclone-crypt";

// Remotely Save の rclone-base64 方式と互換: fileNameEnc="base64" 固定、salt常に空文字
// (src/encryptRClone.ts の `new CipherRCloneCryptPack("base64")` / `cipher.key(password, "")` に合わせる)
export async function createCipher(password) {
  const cipher = new Cipher("base64");
  await cipher.key(password, "");

  return {
    encryptPath(relPath) {
      return cipher.encryptFileName(relPath.replace(/\\/g, "/"));
    },
    decryptPath(encPath) {
      return cipher.decryptFileName(encPath);
    },
    encryptContent(bytes) {
      return cipher.encryptData(bytes);
    },
    decryptContent(bytes) {
      return cipher.decryptData(bytes);
    },
  };
}
