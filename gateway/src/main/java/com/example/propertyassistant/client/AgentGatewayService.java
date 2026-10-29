package com.example.propertyassistant.client;

import com.example.propertyassistant.config.GatewayProperties;
import com.example.propertyassistant.dto.AssistantQueryRequest;
import com.example.propertyassistant.dto.AssistantQueryResponse;
import com.example.propertyassistant.exception.UpstreamAgentException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatusCode;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Service;
import org.springframework.web.client.ResourceAccessException;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

import java.io.IOException;

/**
 * 调用 Python Agent。网关只做协议适配，不承载业务计算。
 */
@Service
public class AgentGatewayService {

    private static final Logger log = LoggerFactory.getLogger(AgentGatewayService.class);

    private final RestClient restClient;
    private final GatewayProperties properties;
    private final ObjectMapper objectMapper;

    public AgentGatewayService(
            RestClient restClient,
            GatewayProperties properties,
            ObjectMapper objectMapper
    ) {
        this.restClient = restClient;
        this.properties = properties;
        this.objectMapper = objectMapper;
    }

    public AssistantQueryResponse query(AssistantQueryRequest request, String requestId) {
        try {
            return restClient.post()
                    .uri(properties.queryPath())
                    .header("X-Request-Id", requestId)
                    .contentType(MediaType.APPLICATION_JSON)
                    .accept(MediaType.APPLICATION_JSON)
                    .body(request)
                    .exchange((clientRequest, clientResponse) -> {
                        HttpStatusCode status = clientResponse.getStatusCode();
                        byte[] body = clientResponse.getBody().readAllBytes();
                        if (!status.is2xxSuccessful()) {
                            throw new UpstreamAgentException(
                                    status.value(),
                                    extractUpstreamMessage(body, status.value()),
                                    requestId
                            );
                        }
                        return objectMapper.readValue(body, AssistantQueryResponse.class);
                    });
        } catch (UpstreamAgentException exception) {
            log.warn("Agent 返回异常 requestId={} upstreamStatus={}",
                    requestId, exception.getUpstreamStatus());
            throw exception;
        } catch (ResourceAccessException exception) {
            log.error("无法连接 Python Agent requestId={}", requestId, exception);
            throw new UpstreamAgentException(503, "Python Agent 服务不可达", requestId);
        } catch (RestClientException exception) {
            log.error("Agent 调用失败 requestId={}", requestId, exception);
            throw new UpstreamAgentException(502, "Python Agent 调用失败", requestId);
        }
    }

    private String extractUpstreamMessage(byte[] body, int status) {
        try {
            JsonNode root = objectMapper.readTree(body);
            JsonNode message = root.path("error").path("message");
            if (!message.isMissingNode() && !message.asText().isBlank()) {
                return message.asText();
            }
        } catch (IOException ignored) {
            // 非 JSON 错误体直接使用通用提示，避免把上游 HTML 泄漏给调用方。
        }
        return "Python Agent 调用失败，HTTP " + status;
    }
}
