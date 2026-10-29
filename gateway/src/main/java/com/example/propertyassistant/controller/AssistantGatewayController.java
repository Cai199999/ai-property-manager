package com.example.propertyassistant.controller;

import com.example.propertyassistant.client.AgentDownloadService;
import com.example.propertyassistant.client.AgentGatewayService;
import com.example.propertyassistant.config.RequestIdFilter;
import com.example.propertyassistant.dto.AssistantQueryRequest;
import com.fasterxml.jackson.databind.JsonNode;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.validation.Valid;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestPart;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;

import java.util.List;

/**
 * 对前端暴露的稳定入口。
 *
 * 动态查询、表目录和上传接口统一返回 JsonNode，Java 不感知业务字段；
 * 下载接口直接把上游文件流复制给浏览器。
 */
@RestController
@RequestMapping("/api/v1/assistant")
public class AssistantGatewayController {

    private final AgentGatewayService agentGatewayService;
    private final AgentDownloadService agentDownloadService;

    public AssistantGatewayController(
            AgentGatewayService agentGatewayService,
            AgentDownloadService agentDownloadService
    ) {
        this.agentGatewayService = agentGatewayService;
        this.agentDownloadService = agentDownloadService;
    }

    @PostMapping("/query")
    public ResponseEntity<JsonNode> query(
            @Valid @RequestBody AssistantQueryRequest request,
            HttpServletRequest servletRequest
    ) {
        JsonNode response = agentGatewayService.query(request, requestId(servletRequest));
        return ResponseEntity.ok(response);
    }

    @PostMapping("/query/code")
    public ResponseEntity<JsonNode> codeQuery(
            @Valid @RequestBody AssistantQueryRequest request,
            HttpServletRequest servletRequest
    ) {
        JsonNode response = agentGatewayService.codeQuery(request, requestId(servletRequest));
        return ResponseEntity.ok(response);
    }

    @GetMapping("/tables")
    public ResponseEntity<JsonNode> tables(HttpServletRequest servletRequest) {
        JsonNode response = agentGatewayService.listTables(requestId(servletRequest));
        return ResponseEntity.ok(response);
    }

    @PostMapping(
            value = "/tables/upload",
            consumes = MediaType.MULTIPART_FORM_DATA_VALUE
    )
    public ResponseEntity<JsonNode> upload(
            @RequestPart("file") List<MultipartFile> files,
            HttpServletRequest servletRequest
    ) {
        JsonNode response = agentGatewayService.uploadTables(
                files,
                requestId(servletRequest)
        );
        return ResponseEntity.ok(response);
    }

    @GetMapping("/download/{filename:.+}")
    public void download(
            @PathVariable String filename,
            HttpServletRequest servletRequest,
            HttpServletResponse servletResponse
    ) {
        agentDownloadService.stream(
                filename,
                requestId(servletRequest),
                servletResponse
        );
    }

    private String requestId(HttpServletRequest request) {
        Object value = request.getAttribute(RequestIdFilter.ATTRIBUTE_NAME);
        return value instanceof String requestId ? requestId : "unknown";
    }
}
