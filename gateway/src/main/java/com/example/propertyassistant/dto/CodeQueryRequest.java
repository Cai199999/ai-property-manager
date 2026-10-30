package com.example.propertyassistant.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

/**
 * 生成式代码查询请求（支持 BYOK）。
 *
 * <p>providerType、userApiKey、modelName 均为可选，网关不做默认值和业务判断，
 * 原样透传给 Python Agent；字段名经 spring.jackson SNAKE_CASE 序列化为
 * provider_type、user_api_key、model_name。</p>
 *
 * <p>安全约束：userApiKey 只允许出现在请求体中，网关不记录请求体，
 * 也不会把它写入任何日志或错误响应。</p>
 */
public record CodeQueryRequest(
        @NotBlank(message = "message 不能为空")
        @Size(min = 2, max = 500, message = "message 长度必须在 2~500 之间")
        String message,

        @Size(max = 100, message = "property_name 最多 100 个字符")
        String propertyName,

        @Size(max = 32, message = "provider_type 最多 32 个字符")
        String providerType,

        @Size(max = 512, message = "user_api_key 最多 512 个字符")
        String userApiKey,

        @Size(max = 128, message = "model_name 最多 128 个字符")
        String modelName
) {
}
