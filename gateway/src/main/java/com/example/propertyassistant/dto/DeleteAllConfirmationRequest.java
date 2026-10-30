package com.example.propertyassistant.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

/**
 * 删除全部表的二次确认请求。
 *
 * <p>令牌由 Python Agent 在 /query/code 识别到 delete_all_tables 时签发，
 * 网关只负责校验长度并原样透传，不生成、不缓存、不记录令牌内容。</p>
 */
public record DeleteAllConfirmationRequest(
        @NotBlank(message = "confirmation_token 不能为空")
        @Size(min = 20, max = 256, message = "confirmation_token 长度不合法")
        String confirmationToken
) {
}
