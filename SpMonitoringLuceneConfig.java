package com.ubs.spasa.batch.jobs;

import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Primary;

/**
 * Lucene infrastructure bean definitions for the SP Monitoring Search Index job.
 * <p>
 * Provides:
 * <ul>
 *   <li>Lucene config service (primary), index creator, and index reader.</li>
 * </ul>
 * These beans are consumed by {@link SpMonitoringSearchIndexJob}.
 * </p>
 */
@Configuration
public class SpMonitoringLuceneConfig {

    private static final String CONFIG_NAME = "sp_monitoring_search";

    // ── Routing Lucene Config Service (primary) ──────────────────────────────

    @Bean
    @Primary
    public SPMonitoringLuceneConfigServiceImpl spMonitoringLuceneConfigService(
            @Qualifier("luceneConfigServiceImpl") LuceneConfigService delegate,
            SPMonitoringSearchService spMonitoringSearchService) {
        return new SPMonitoringLuceneConfigServiceImpl(delegate, spMonitoringSearchService);
    }

    // ── Lucene Index Creator & Reader ────────────────────────────────────────

    @Bean
    public SPMonitoringLuceneIndexCreator spMonitoringLuceneCreator() {
        SPMonitoringLuceneIndexCreator creator = new SPMonitoringLuceneIndexCreator();
        creator.setConfigName(CONFIG_NAME);
        return creator;
    }

    @Bean
    public StandardLuceneIndexReader spMonitoringLuceneReader() {
        StandardLuceneIndexReader reader = new StandardLuceneIndexReader();
        reader.setConfigName(CONFIG_NAME);
        return reader;
    }
}
