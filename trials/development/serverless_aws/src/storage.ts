const AWS = require('aws-sdk');

const s3 = new AWS.S3();

export async function saveFile(bucket: string, key: string, data: Buffer) {
  return s3.putObject({ Bucket: bucket, Key: key, Body: data }).promise();
}
