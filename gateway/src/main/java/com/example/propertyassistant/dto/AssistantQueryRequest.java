package com.example.propertyassistant.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

/**
 * 自然语言查询请求。Jackson 已配置 snake_case，propertyName 对外为 property_name。
 */
public record AssistantQueryRequest(
        @NotBlank(message = "message 不能为空")
        @Size(min = 2, max = 500, message = "message 长度必须在 2~500 之间")
        String message,

        @Size(max = 100, message = "property_name 最多 100 个字符")
        String propertyName
) {
}
