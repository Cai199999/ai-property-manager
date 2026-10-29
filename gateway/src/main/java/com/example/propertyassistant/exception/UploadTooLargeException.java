package com.example.propertyassistant.exception;

/**
 * 上传文件超过网关配置的大小上限。
 */
public class UploadTooLargeException extends RuntimeException {

    public UploadTooLargeException(String message) {
        super(message);
    }
}
