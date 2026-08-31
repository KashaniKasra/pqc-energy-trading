'use strict';

const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');
const { WorkloadModuleBase } = require('@hyperledger/caliper-core');

class SetWorkload extends WorkloadModuleBase {

    constructor() {
        super();
        this.txIndex = 0;
        this.contractId = 'simplekv';
        this.roundLabel = 'unknown';
        this.configLabel = process.env.E1_CONFIG || 'unknown';
        this.heightsFile = path.resolve(
            process.cwd(),
            '../../../raw/e1',
            `${this.configLabel}_block_heights.csv`
        );
    }

    getLedgerHeight() {
        const output = execFileSync(
            'docker',
            [
                'exec',
                'peer0.org1.example.com',
                'peer',
                'channel',
                'getinfo',
                '-c',
                'energychannel'
            ],
            {
                encoding: 'utf8',
                stdio: ['ignore', 'pipe', 'ignore']
            }
        );

        const match = output.match(/"height":(\d+)/);

        if (!match) {
            throw new Error('Could not determine Fabric ledger height');
        }

        return Number(match[1]);
    }

    recordHeight(marker) {
        const height = this.getLedgerHeight();
        fs.appendFileSync(
            this.heightsFile,
            `${marker},${height}\n`
        );
    }

    async initializeWorkloadModule(workerIndex, totalWorkers, roundIndex, roundArguments, sutAdapter, sutContext) {
        await super.initializeWorkloadModule(
            workerIndex,
            totalWorkers,
            roundIndex,
            roundArguments,
            sutAdapter,
            sutContext
        );

        if (roundArguments.contractId) {
            this.contractId = roundArguments.contractId;
        }

        if (roundArguments.roundLabel) {
            this.roundLabel = roundArguments.roundLabel;
        }

        if (this.workerIndex === 0) {
            if (this.roundIndex === 0) {
                fs.mkdirSync(path.dirname(this.heightsFile), { recursive: true });
                fs.writeFileSync(this.heightsFile, 'marker,height\n');
            }

            this.recordHeight(`before_${this.roundLabel}`);
        }
    }

    async submitTransaction() {
        const key = `key_${this.workerIndex}_${this.roundIndex}_${this.txIndex}`;
        const value = `value_${this.txIndex}`;

        const request = {
            contractId: this.contractId,
            contractFunction: 'Set',
            invokerIdentity: 'User1',
            contractArguments: [key, value],
            readOnly: false,
            roundLabel: this.roundLabel
        };

        this.txIndex++;

        await this.sutAdapter.sendRequests(request);
    }

    async cleanupWorkloadModule() {
        if (this.workerIndex === 0) {
            this.recordHeight(`after_${this.roundLabel}`);
        }

        await super.cleanupWorkloadModule();
    }
}

function createWorkloadModule() {
    return new SetWorkload();
}

module.exports.createWorkloadModule = createWorkloadModule;