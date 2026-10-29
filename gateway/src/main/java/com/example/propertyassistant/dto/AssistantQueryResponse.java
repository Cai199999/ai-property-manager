package com.example.propertyassistant.dto;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;

import java.time.LocalDate;
import java.util.List;
import java.util.Map;

/**
 * 与 FastAPI 响应对齐；summary/meta 用 Map 保留 Python 侧的可扩展字段。
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public record AssistantQueryResponse(
        String requestId,
        String query,
        String dataSource,
        TimeRange timeRange,
        ParserInfo parser,
        Map<String, Object> summary,
        List<Map<String, Object>> recordsPreview,
        Map<String, Object> meta
) {
    @JsonIgnoreProperties(ignoreUnknown = true)
    public record TimeRange(LocalDate startDate, LocalDate endDate) {
    }

    @JsonIgnoreProperties(ignoreUnknown = true)
    public record ParserInfo(
            String engine,
            String model,
            Double confidence,
            String reason
    ) {
    }
}
