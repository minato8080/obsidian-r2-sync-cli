import { Cipher } from "@fyears/rclone-crypt";

// Remotely Save の rclone-base64 方式と互換: fileNameEnc="base64" 固定、salt常に空文字
// (src/encryptRClone.ts の `new CipherRCloneCryptPack("base64")` / `cipher.key(password, "")` に合わせる)
export async function createCipher(password, mode = "rclone-base64") {
  if (mode === "plain") return { encryptPath: async (value) => value.replace(/\\/g, "/"), decryptPath: async (value) => value, encryptContent: async (bytes) => bytes, decryptContent: async (bytes) => bytes };
  const cipher = new Cipher("base64");
  await cipher.key(password, "");

  return {
    async encryptPath(relPath) {
      return cipher.encryptFileName(relPath.replace(/\\/g, "/"));
    },
    async decryptPath(encPath) {
      return cipher.decryptFileName(encPath);
    },
    async encryptContent(bytes) {
      return cipher.encryptData(bytes);
    },
    async decryptContent(bytes) {
      return cipher.decryptData(bytes);
    },
  };
}
