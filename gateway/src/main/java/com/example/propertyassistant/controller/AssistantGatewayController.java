package com.example.propertyassistant.controller;

import com.example.propertyassistant.client.AgentGatewayService;
import com.example.propertyassistant.config.RequestIdFilter;
import com.example.propertyassistant.dto.AssistantQueryRequest;
import com.example.propertyassistant.dto.AssistantQueryResponse;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/v1/assistant")
public class AssistantGatewayController {

    private final AgentGatewayService agentGatewayService;

    public AssistantGatewayController(AgentGatewayService agentGatewayService) {
        this.agentGatewayService = agentGatewayService;
    }

    @PostMapping("/query")
    public ResponseEntity<AssistantQueryResponse> query(
            @Valid @RequestBody AssistantQueryRequest request,
            HttpServletRequest servletRequest
    ) {
        String requestId = (String) servletRequest.getAttribute(RequestIdFilter.ATTRIBUTE_NAME);
        AssistantQueryResponse response = agentGatewayService.query(request, requestId);
        return ResponseEntity.ok(response);
    }
}
