package com.example.propertyassistant.exception;

/**
 * 上传文件缺失、类型不合法或文件名异常。
 */
public class InvalidUploadException extends RuntimeException {

    public InvalidUploadException(String message) {
        super(message);
    }
}
