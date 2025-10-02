CREATE OR ALTER PROCEDURE RG_OprationalBackup.dbo.USP_QC_QA_Report_Deduped
    @ReportIdCsv NVARCHAR(MAX) = NULL,
    @FromDate DATETIME = NULL,
    @ToDate DATETIME = NULL
AS
BEGIN
    SET NOCOUNT ON;

    ;WITH Src AS (
        SELECT
            qa.ReportID,
            qa.AccountName,
            qa.CustomerId,
            qa.UserId,
            qa.TestCaseID,
            qa.TestCaseInfo,
            qa.TCPriority,
            qa.TCStatus,
            qa.TotalCount,
            qa.TCFailureCount,
            qa.WebsiteID,
            qa.Requestsegmentid,
            qa.MaxReportdetailID,
            qa.LogDate,
            qa.errordetails,
            qa.ExecutionTimeSeconds,
            qa.ExecutionTimeFormatted,
            StatusRank = CASE LOWER(qa.TCStatus)
                WHEN 'fail' THEN 4
                WHEN 'error' THEN 3
                WHEN 'pass' THEN 2
                WHEN 'stats' THEN 2
                WHEN 'ignore' THEN 1
                ELSE 0
            END
        FROM RG_OprationalBackup.dbo.RG_QC_TestRunAutomate_QA qa WITH (NOLOCK)
        WHERE
            (@FromDate IS NULL OR qa.LogDate >= @FromDate)
            AND (@ToDate IS NULL OR qa.LogDate < @ToDate)
            AND (
                @ReportIdCsv IS NULL OR
                qa.ReportID IN (
                    SELECT TRY_CAST(value AS BIGINT)
                    FROM STRING_SPLIT(@ReportIdCsv, ',')
                    WHERE TRY_CAST(value AS BIGINT) IS NOT NULL
                )
            )
    ),
    Ranked AS (
        SELECT *,
            ROW_NUMBER() OVER (
                PARTITION BY UserId, AccountName, ReportID, TestCaseID
                ORDER BY StatusRank DESC, LogDate DESC, MaxReportdetailID DESC
            ) AS rn
        FROM Src
    )
    SELECT
        ReportID, AccountName, CustomerId, UserId, TestCaseID, TestCaseInfo, TCPriority, TCStatus,
        TotalCount, TCFailureCount, WebsiteID, Requestsegmentid, MaxReportdetailID, LogDate, errordetails,
        ExecutionTimeSeconds, ExecutionTimeFormatted
    FROM Ranked
    WHERE rn = 1
    ORDER BY AccountName, ReportID, TestCaseID;
END

