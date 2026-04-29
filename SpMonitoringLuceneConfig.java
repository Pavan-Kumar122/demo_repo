package com.ubs.spasa.batch.jobs;

import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Primary;

/**
 * Lucene infrastructure and supporting bean definitions for the
 * SP Monitoring Search Index batch job.
 * <p>
 * Provides:
 * <ul>
 *   <li>Lucene config service, index creator, and index reader.</li>
 *   <li>Row mapper, item writer, and job parameter validator.</li>
 *   <li>Job context support.</li>
 * </ul>
 * These beans are consumed by {@link SpMonitoringSearchIndexJob}.
 * </p>
 */
@Configuration
public class SpMonitoringLuceneConfig {

    private static final String CONFIG_NAME = "sp_monitoring_search";

    private final LuceneConfigService luceneConfigServiceDelegate;
    private final SPMonitoringSearchService spMonitoringSearchService;

    public SpMonitoringLuceneConfig(
            @Qualifier("luceneConfigServiceImpl") LuceneConfigService luceneConfigServiceDelegate,
            SPMonitoringSearchService spMonitoringSearchService) {
        this.luceneConfigServiceDelegate = luceneConfigServiceDelegate;
        this.spMonitoringSearchService = spMonitoringSearchService;
    }

    // ── Routing Lucene Config Service (primary) ──────────────────────────────

    @Bean
    @Primary
    public SPMonitoringLuceneConfigServiceImpl spMonitoringLuceneConfigService() {
        return new SPMonitoringLuceneConfigServiceImpl(
                luceneConfigServiceDelegate, spMonitoringSearchService);
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

    // ── Row Mapper, Writer & Validator ───────────────────────────────────────

    @Bean
    public SPMonitoringBeanRowMapper spMonitoringBeanRowMapper() {
        return new SPMonitoringBeanRowMapper();
    }

    @Bean
    public SPMonitoringIndexDataWriter spMonitoringIndexDataWriter() {
        return new SPMonitoringIndexDataWriter();
    }

    @Bean
    public SPMonitoringIndexJobParamValidator spMonitoringIndexJobParamValidator() {
        return new SPMonitoringIndexJobParamValidator();
    }

    // ── Job Context Support ─────────────────────────────────────────────────

    @Bean
    public JobContextSupport jobContextSupport() {
        JobContextSupport support = new JobContextSupport();
        support.setJobName(JobNames.SP_MONITORING_SEARCH_INDEX_JOB);
        return support;
    }
}
