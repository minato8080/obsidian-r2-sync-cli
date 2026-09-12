import {
  S3Client,
  ListObjectsV2Command,
  GetObjectCommand,
  HeadObjectCommand,
  DeleteObjectCommand,
  HeadBucketCommand,
} from "@aws-sdk/client-s3";
import { Upload } from "@aws-sdk/lib-storage";

export function createR2Client(r2Config) {
  const client = new S3Client({
    region: "auto",
    endpoint: r2Config.endpoint,
    credentials: {
      accessKeyId: r2Config.accessKeyId,
      secretAccessKey: r2Config.secretAccessKey,
    },
  });

  async function checkConnection() {
    await client.send(new HeadBucketCommand({ Bucket: r2Config.bucket }));
  }

  async function listAll() {
    const items = [];
    let token;
    do {
      const res = await client.send(
        new ListObjectsV2Command({
          Bucket: r2Config.bucket,
          Prefix: r2Config.prefix || undefined,
          ContinuationToken: token,
        })
      );
      for (const obj of res.Contents ?? []) {
        items.push({ key: obj.Key, etag: obj.ETag, size: obj.Size, lastModified: obj.LastModified });
      }
      token = res.IsTruncated ? res.NextContinuationToken : undefined;
    } while (token);
    return items;
  }

  async function getObject(key) {
    const res = await client.send(new GetObjectCommand({ Bucket: r2Config.bucket, Key: key }));
    const bytes = await res.Body.transformToByteArray();
    return { bytes, metadata: res.Metadata ?? {}, etag: res.ETag };
  }

  async function headObject(key) {
    const res = await client.send(new HeadObjectCommand({ Bucket: r2Config.bucket, Key: key }));
    return { metadata: res.Metadata ?? {}, etag: res.ETag };
  }

  async function putObject(key, bytes, metadata) {
    const upload = new Upload({
      client,
      params: {
        Bucket: r2Config.bucket,
        Key: key,
        Body: Buffer.from(bytes),
        Metadata: metadata,
      },
    });
    const result = await upload.done();
    return { etag: result.ETag };
  }

  async function deleteObject(key) {
    await client.send(new DeleteObjectCommand({ Bucket: r2Config.bucket, Key: key }));
  }

  return { checkConnection, listAll, getObject, headObject, putObject, deleteObject };
}
