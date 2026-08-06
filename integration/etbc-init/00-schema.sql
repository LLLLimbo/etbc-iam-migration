CREATE DATABASE IF NOT EXISTS `etbc_source` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
USE `etbc_source`;

CREATE TABLE `biz_participant` (
  `id` int NOT NULL,
  `createDate` datetime DEFAULT NULL,
  `lastUpdateDate` datetime DEFAULT NULL,
  `ownership` varchar(20) NOT NULL,
  `name` varchar(50) NOT NULL,
  `description` varchar(100) DEFAULT NULL,
  `code` varchar(50) DEFAULT NULL,
  `contact` varchar(20) DEFAULT NULL,
  `mobilePhone` varchar(11) DEFAULT NULL,
  `officePhone` varchar(20) DEFAULT NULL,
  `status_id` int NOT NULL,
  `type_id` int DEFAULT NULL,
  `tId` varchar(50) NOT NULL,
  `industryCode` varchar(64) DEFAULT NULL,
  `newTenantId` varchar(50) DEFAULT NULL,
  `unifiedSocialCreditCode` varchar(50) DEFAULT NULL,
  `iam_lessee_id` varchar(36) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_tenant_id` (`tId`)
) ENGINE=InnoDB;

CREATE TABLE `sys_orgnization` (
  `id` int NOT NULL,
  `createDate` datetime DEFAULT NULL,
  `lastUpdateDate` datetime DEFAULT NULL,
  `ownership` varchar(20) NOT NULL,
  `address` varchar(50) DEFAULT NULL,
  `code` varchar(50) DEFAULT NULL,
  `leader` varchar(20) DEFAULT NULL,
  `name` varchar(50) NOT NULL,
  `no` varchar(20) DEFAULT NULL,
  `participantCode` varchar(20) DEFAULT NULL,
  `phone` varchar(25) DEFAULT NULL,
  `remark` varchar(100) DEFAULT NULL,
  `parentOrg_id` int DEFAULT NULL,
  `type_id` int NOT NULL,
  `userId` int DEFAULT NULL,
  `addressCoordinate` varchar(50) DEFAULT NULL,
  `orgNo` varchar(30) DEFAULT NULL,
  `systemCode` varchar(50) DEFAULT NULL,
  `dataPermission` varchar(20) DEFAULT NULL,
  `provinceCode` varchar(20) DEFAULT NULL,
  `cityCode` varchar(20) DEFAULT NULL,
  `easCode` varchar(128) DEFAULT NULL,
  `cisorginfo` varchar(255) DEFAULT NULL,
  `deleted` int NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB;

CREATE TABLE `sys_user` (
  `id` int NOT NULL,
  `createDate` datetime DEFAULT NULL,
  `lastUpdateDate` datetime DEFAULT NULL,
  `ownership` varchar(20) NOT NULL,
  `accountLockedTime` datetime DEFAULT NULL,
  `birthday` datetime DEFAULT NULL,
  `csremail` varchar(30) DEFAULT NULL,
  `jobTitle` varchar(30) DEFAULT NULL,
  `loginName` varchar(50) NOT NULL,
  `loginOrNot` bit(1) DEFAULT NULL,
  `loginPwd` varchar(50) DEFAULT NULL,
  `loginRetryTimes` int DEFAULT NULL,
  `mobilePhone` varchar(15) DEFAULT NULL,
  `name` varchar(20) NOT NULL,
  `workPhone` varchar(20) DEFAULT NULL,
  `gender_id` int DEFAULT NULL,
  `status_id` int NOT NULL,
  `systemCode` varchar(30) DEFAULT NULL,
  `orgnization_id` int NOT NULL,
  `jobNumber` varchar(50) DEFAULT NULL,
  `commonPlace` varchar(500) DEFAULT NULL,
  `accountOrgNo` varchar(20) DEFAULT NULL,
  `agentNo` int DEFAULT NULL,
  `affiliateSubAccount_id` int DEFAULT NULL,
  `affiliateAccountAppQueue_id` int DEFAULT NULL,
  `affiliateAccountAppQueueNo` varchar(30) DEFAULT NULL,
  `wxUserId` varchar(20) DEFAULT NULL,
  `nailUserId` varchar(20) DEFAULT NULL,
  `isUserType` tinyint NOT NULL DEFAULT 7,
  `headImg` varchar(300) DEFAULT NULL,
  `loginPwdEncrypt` varchar(100) DEFAULT NULL,
  `identyCard` varchar(64) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_login_name` (`loginName`)
) ENGINE=InnoDB;

CREATE TABLE `sys_user_orgnization` (
  `user_id` int NOT NULL,
  `orgnization_id` int NOT NULL
) ENGINE=InnoDB;

INSERT INTO `biz_participant` (
  `id`, `createDate`, `lastUpdateDate`, `ownership`, `name`, `description`, `code`,
  `contact`, `mobilePhone`, `officePhone`, `status_id`, `type_id`, `tId`,
  `industryCode`, `newTenantId`, `unifiedSocialCreditCode`, `iam_lessee_id`
) VALUES (
  1, '2026-07-30 08:00:00', '2026-07-30 08:30:00', '1001-tenant',
  'Synthetic Tenant', 'Synthetic Compose fixture', 'SYN-001', 'Synthetic Contact',
  '13900000001', '057100000001', 2, 1, 'synthetic-tenant-001', 'SYNTHETIC', NULL,
  '91330000SYNTHETIC01', NULL
);

INSERT INTO `sys_orgnization` (
  `id`, `createDate`, `lastUpdateDate`, `ownership`, `address`, `code`, `leader`,
  `name`, `no`, `participantCode`, `phone`, `remark`, `parentOrg_id`, `type_id`,
  `userId`, `addressCoordinate`, `orgNo`, `systemCode`, `dataPermission`,
  `provinceCode`, `cityCode`, `easCode`, `cisorginfo`, `deleted`
) VALUES
  (10, '2026-07-30 08:00:00', '2026-07-30 08:30:00', '1001-root',
   'Synthetic Root Address', 'ROOT-CODE', 'Root Leader', 'Synthetic Root', 'ROOT',
   'SYN-001', '057100000010', 'Root note', 0, 1, NULL, NULL, 'ROOT-OUT', 'ROOT-SYS',
   '1001-root', '330000', '330100', 'ROOT-EAS', NULL, 0),
  (11, '2026-07-30 08:01:00', '2026-07-30 08:31:00', '1001-child',
   'Synthetic Child Address', 'CHILD-CODE', 'Child Leader', 'Synthetic Child', 'CHILD',
   'SYN-001', '057100000011', 'Child note', 10, 5, NULL, NULL, 'CHILD-OUT', 'CHILD-SYS',
   '1001-root', '330000', '330100', 'CHILD-EAS', NULL, 0);

INSERT INTO `sys_user` (
  `id`, `createDate`, `lastUpdateDate`, `ownership`, `accountLockedTime`, `csremail`,
  `loginName`, `loginOrNot`, `loginPwd`, `loginRetryTimes`, `mobilePhone`, `name`,
  `workPhone`, `gender_id`, `status_id`, `systemCode`, `orgnization_id`, `jobNumber`,
  `isUserType`, `headImg`, `loginPwdEncrypt`, `identyCard`, `birthday`, `jobTitle`,
  `commonPlace`, `accountOrgNo`, `agentNo`, `affiliateSubAccount_id`,
  `affiliateAccountAppQueue_id`, `affiliateAccountAppQueueNo`, `wxUserId`, `nailUserId`
) VALUES
  (100, '2026-07-30 08:02:00', '2026-07-30 08:32:00', '1001-staff', NULL, NULL,
   'synthetic_admin', b'1', 'source-only-sentinel-a', 0, '13900000100', 'Synthetic Admin',
   '057100000100', NULL, 2, 'STAFF-SYS', 11, 'SYN-100', 2, NULL,
   'source-only-encrypted-sentinel-a', 'SYNTHETIC-ID-100', '1990-01-01 00:00:00',
   'Synthetic Operator', 'Synthetic Place', 'SYN-ORG', 7, 700, 800, 'SYN-Q',
   'wx-syn-100', 'nail-syn-100'),
  (101, '2026-07-30 08:03:00', '2026-07-30 08:33:00', '1001-staff',
   '2026-07-30 08:03:30', 'female@example.invalid', 'synthetic_staff', b'1',
   'source-only-sentinel-b', 1, '13900000101', 'Synthetic Staff', '057100000101', 2, 2,
   'STAFF-SYS', 11, 'SYN-101', 7, NULL, 'source-only-encrypted-sentinel-b',
   'SYNTHETIC-ID-101', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);

-- This deliberately contradicts sys_user.orgnization_id and must be ignored.
INSERT INTO `sys_user_orgnization` (`user_id`, `orgnization_id`) VALUES (100, 10);
